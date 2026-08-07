# README.md
SQLiteからRDS for PostgreSQLへの移行検証に使用したCloudFormationのテンプレート、各種スクリプトを格納しています。

# SQLite → RDS for PostgreSQL 移行検証キット（pgloader）

SQLiteで運用しているデータベースを、Amazon RDS for PostgreSQL へ移行できるかを検証するための使い捨て検証環境（CloudFormation）と、検証用スクリプト一式です。

[ブログ記事: SQLiteからRDS for PostgreSQLへの移行をpgloaderで検証してみた](https://dev.classmethod.jp/articles/myan-aws-sqlite-rds-verify/) の手順で使用したファイルをそのまま公開しています。

## 概要

- [pgloader](https://pgloader.io/) を使って、SQLiteのスキーマ・データをAmazon RDS for PostgreSQL へ移行するデモです
- 実データではなく、生成スクリプトで作った**架空のサンプルデータ**（見積書管理・経費仕訳を模したスキーマ）で動作確認します
- 移行そのものだけでなく、**移行後に何が起きるか（NOT NULL制約が失われる、等）**を確認することを主眼にしています

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
| `scripts/compare_migration.py` | 移行前後のデータ（行数・数値列の合計・サンプル行）を突合するPythonスクリプト |
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

### 7. 移行結果を確認する

```bash
sudo -u ec2-user python3 /home/ec2-user/scripts/compare_migration.py \
  --sqlite /home/ec2-user/data/sample.db \
  --pg "postgresql://testadmin:<パスワード>@<RDSEndpoint>:5432/testdb?sslmode=require"
```

### 8. 後片付け（重要）

**検証が終わったら必ずスタックを削除してください**（RDSが起動している間は課金が発生します）。

```bash
aws cloudformation delete-stack --stack-name sqlite-pg-verify
```

## 詰まりやすいポイント

詳しくはブログ記事を参照してください。要点だけ書くと：

- 接続URLには **`?sslmode=prefer`** を付けてください。`sslmode=require` だとpgloaderが証明書検証まで行い、`SSL verify error: 19 X509_V_ERR_SELF_SIGNED_CERT_IN_CHAIN` で失敗します（`--no-ssl-cert-verification` フラグはコマンドファイル利用時には効きませんでした）
- 移行後、**`NOT NULL`制約はすべて失われます**。`ALTER TABLE ... ALTER COLUMN ... SET NOT NULL` での貼り直しが必要です
- `UNIQUE`制約はインデックスとしては再現されますが、`pg_constraint` には登録されません
- 外部キー制約は移行元に無ければ当然作られません。手動で `ADD CONSTRAINT` してください

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
