# README.md
SQLiteからRDS for PostgreSQLへの移行検証に使用したCloudFormationのテンプレート、各種スクリプトを格納しています。

# SQLite → RDS for PostgreSQL 移行検証キット（pgloader）

SQLiteで運用しているデータベースを、Amazon RDS for PostgreSQL へ移行できるかを検証するための使い捨て検証環境（CloudFormation）と、検証用スクリプト一式です。

[ブログ記事: SQLiteからRDS for PostgreSQLへの移行をpgloaderで検証してみた（ハマりどころ全部載せ）](<ブログ公開後にURLを追記>) の手順で使用したファイルをそのまま公開しています。

## 概要

- [pgloader](https://pgloader.io/) を使って、SQLiteのスキーマ・データをAmazon RDS for PostgreSQL へ移行するデモです
- 実データではなく、生成スクリプトで作った**架空のサンプルデータ**（見積書管理・経費仕訳を模したスキーマ）で動作確認します
- 移行そのものだけでなく、**移行後に何が起きるか（今回の環境ではNOT NULL制約が反映されなかった、等）**を確認することを主眼にしています

## 構成

```
[EC2 (SSM Session Managerで接続, Docker/pgloader/sqlite3)] --5432--> [RDS for PostgreSQL]
```

CloudFormationテンプレート1本で、検証に必要なリソースを一式構築します。

| リソース | 内容 |
|---|---|
| VPC | パブリックサブネット×1、プライベートサブネット×2（RDSのDBサブネットグループ要件で最低2AZ必要） |
| EC2 | Amazon Linux 2023 / t3.micro。作業ホスト。**SSHポートは開放せず、SSM Session Managerのみで接続** |
| RDS | PostgreSQL 16 / db.t3.micro。プライベートサブネット、パブリックアクセス不可 |
| S3 | スクリプト転送用バケット。EC2のIAMロールは特定プレフィックス（既定`scripts/`）のみ読み取り可 |
| IAM | EC2用ロール（SSM用の`AmazonSSMManagedInstanceCore`＋S3読み取り） |

**使い捨て環境**として設計しています（RDSは`DeletionPolicy: Delete`・最終スナップショットなし）。検証用途以外では使わないでください。

## 含まれるファイル

| ファイル | 役割 |
|---|---|
| `cfn/verify-environment.yaml` | 検証環境一式を構築するCloudFormationテンプレート |
| `scripts/generate_sample_sqlite.py` | サンプルSQLite DBを生成するPythonスクリプト |
| `scripts/compare_migration.py` | 移行前後のデータ（行数・数値列の合計・サンプル行）を突合するPythonスクリプト（全件完全一致を保証するものではありません） |
| `pgloader/migrate.load` | pgloaderの移行定義ファイル |

## 前提条件

- AWS CLI が設定済みであること（`aws configure` 済み、CloudFormation・EC2・RDS・S3・IAMロール作成権限があること）
- [Session Manager プラグイン](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) がインストール済みであること（`aws ssm start-session` を使うため）
- Docker が使える環境（EC2上。テンプレートのUserDataで自動インストールされます）

## 使い方

### 1. 環境をデプロイする

```bash
aws cloudformation deploy \
  --template-file cfn/verify-environment.yaml \
  --stack-name sqlite-pg-verify \
  --parameter-overrides DBMasterPassword='<強固なパスワードを指定>' \
  --capabilities CAPABILITY_NAMED_IAM
```

デプロイ完了後、Outputsから `RDSEndpoint`・`EC2InstanceId`・`TransferBucketName` を控えます。

```bash
aws cloudformation describe-stacks --stack-name sqlite-pg-verify \
  --query "Stacks[0].Outputs" --output table
```

### 2. スクリプトをS3経由でEC2へ配置する

```bash
BUCKET=<TransferBucketName>

aws s3 cp scripts/generate_sample_sqlite.py s3://$BUCKET/scripts/generate_sample_sqlite.py
aws s3 cp scripts/compare_migration.py      s3://$BUCKET/scripts/compare_migration.py
aws s3 cp pgloader/migrate.load             s3://$BUCKET/scripts/migrate.load
```

### 3. EC2へ接続する

```bash
aws ssm start-session --target <EC2InstanceId>
```

### 4. EC2上でファイルをダウンロードし、サンプルデータを生成する

```bash
sudo -u ec2-user aws s3 cp s3://$BUCKET/scripts/generate_sample_sqlite.py /home/ec2-user/scripts/generate_sample_sqlite.py
sudo -u ec2-user aws s3 cp s3://$BUCKET/scripts/compare_migration.py     /home/ec2-user/scripts/compare_migration.py
sudo -u ec2-user aws s3 cp s3://$BUCKET/scripts/migrate.load             /home/ec2-user/data/migrate.load

sudo -u ec2-user python3 /home/ec2-user/scripts/generate_sample_sqlite.py --rows 5000 --out /home/ec2-user/data/sample.db
```

### 5. `migrate.load` のプレースホルダを置き換える

```bash
cd /home/ec2-user/data
read -s -p "Master password: " PGPASS; echo

sed -i \
  -e "s/<RDS_ENDPOINT>/<Outputsで確認したRDSEndpoint>/" \
  -e "s/<DB_MASTER_USERNAME>/testadmin/" \
  -e "s/<MASTER_PASSWORD>/$PGPASS/" \
  migrate.load

unset PGPASS
```

### 6. pgloaderで移行を実行する

```bash
sudo -u ec2-user docker run --rm -v /home/ec2-user/data:/data dimitri/pgloader:latest \
  pgloader /data/migrate.load
```

`dimitri/pgloader:latest` はmasterブランチからの自動ビルドで、固定リリースではありません（今回の実行時点の実体は `3.6.7~devel`）。再現検証や本番移行では、実行前にバージョンとdigestを記録し、digestを固定して使うことを推奨します。

```bash
docker run --rm dimitri/pgloader:latest pgloader --version
docker image inspect dimitri/pgloader:latest --format '{{index .RepoDigests 0}}'
```

### 7. 移行結果を確認する

```bash
sudo -u ec2-user python3 /home/ec2-user/scripts/compare_migration.py \
  --sqlite /home/ec2-user/data/sample.db \
  --pg "postgresql://testadmin:<パスワード>@<RDSEndpoint>:5432/testdb?sslmode=require"
```

このスクリプトが確認しているのは行数・指定した数値列の合計・先頭5件のサンプル値であり、全件の完全一致を保証するものではありません。差分が見つかった場合は終了コード1で終了します。

### 8. 後片付け（重要）

**検証が終わったら必ずスタックを削除してください**（RDSが起動している間は課金が発生します）。

CloudFormationはオブジェクトが入ったS3バケットを自動的には空にしないため、手順2でスクリプトをアップロードしたバケットが残ったままスタックを削除すると `DELETE_FAILED` になります。先にバケットを空にしてから削除してください。詳細は[公式ドキュメント（トラブルシューティング）](https://docs.aws.amazon.com/ja_jp/AWSCloudFormation/latest/UserGuide/troubleshooting.html)をご確認ください。

```bash
aws s3 rm "s3://$BUCKET" --recursive

aws cloudformation delete-stack \
  --stack-name sqlite-pg-verify

aws cloudformation wait stack-delete-complete \
  --stack-name sqlite-pg-verify
```

## 詰まりやすいポイント

詳しくはブログ記事を参照してください。要点だけ書くと：

- 接続URLには **`?sslmode=prefer`** を付けてください。今回利用したDockerイメージのpgloader `3.6.7~devel` で観測した挙動として、`sslmode=require` だとpgloaderが証明書検証まで行い、`SSL verify error: 19 X509_V_ERR_SELF_SIGNED_CERT_IN_CHAIN` で失敗しました（`--no-ssl-cert-verification` フラグはコマンドファイル利用時には効きませんでした）。libpqの`sslmode`と同じ意味とは限らない点にご注意ください。`prefer`はまずSSL接続を試み、失敗時に非SSLへフォールバックし得るモードですが、RDS側の`rds.force_ssl=1`により非SSL接続が拒否されるため、結果的にSSL接続になります。ただし**接続先の真正性は検証されません**。本番では、CA証明書バンドルを使った`verify-full`の利用を推奨します。詳細は[PostgreSQL公式ドキュメント（SSL Support）](https://www.postgresql.org/docs/16/libpq-ssl.html)をご確認ください
- **今回の環境（pgloader `3.6.7~devel` ＋ 本サンプルDB）では、主キー以外の`NOT NULL`制約が反映されませんでした**。pgloaderは`PRAGMA table_info`からNOT NULL情報を取得しているため、全バージョンで必ず失われるとは断定できません。移行後は`information_schema.columns`で必ず確認し、必要であれば`ALTER TABLE ... ALTER COLUMN ... SET NOT NULL`で貼り直してください
- `UNIQUE`制約はインデックスとしては再現されますが、`pg_constraint` には登録されません
- 外部キー制約は移行元に無ければ当然作られません。手動で `ADD CONSTRAINT` してください
- `include drop` は対象テーブルを`CASCADE`で削除します。本番の既存DBに対して実行しないでください。専用の空DB・スキーマへロードするか、事前にテーブルを作成した上で`create no tables`でロードする運用にしてください
- 大規模テーブルへの`SET NOT NULL`は既存データを検査するため、ロックとスキャンの影響を事前に確認してください。詳細は[PostgreSQL公式ドキュメント（ALTER TABLE）](https://www.postgresql.org/docs/16/ddl-alter.html)をご確認ください

## パラメータのカスタマイズ

`cfn/verify-environment.yaml` は以下のパラメータで調整できます。

| パラメータ | 既定値 | 説明 |
|---|---|---|
| `EnvironmentName` | `test-sqlite-pg-migration` | リソース名のプレフィックス |
| `DBMasterUsername` | `testadmin` | RDSマスターユーザー名 |
| `DBEngineVersion` | `16.14` | RDSのPostgreSQLバージョン |
| `DBInstanceClass` | `db.t3.micro` | RDSインスタンスクラス |
| `EC2InstanceType` | `t3.micro` | EC2インスタンスタイプ |
| `S3ObjectPrefix` | `scripts/` | EC2が読み取り可能なS3プレフィックス |

## 注意事項

- 本リポジトリは**検証・学習目的**のものです。本番環境にそのまま使用しないでください（NAT Gatewayなし、シングルAZ、暗号化キーは標準キー使用など、コスト最小化を優先した構成です）
- サンプルデータは架空のものです。実データでの移行時は、実スキーマに合わせて `generate_sample_sqlite.py` の代わりに実際の `.db` ファイルを使用し、`migrate.load` のCASTルール等を見直してください
- AWSリソースの作成・削除に伴う課金は自己責任でお願いします
- 本リポジトリの手順は使い捨ての検証環境を前提に、`migrate.load` へパスワードを直接記述しています。平文が残るため、検証終了後はファイルごと削除し、本番相当の環境では `PGPASSFILE` / `.pgpass` など平文を残さない方法を検討してください
