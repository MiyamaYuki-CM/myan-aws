# 検証用CloudFormationテンプレート（クロスアカウント版）

## デプロイ手順（B → A → B更新 の3ステップ・順序が重要）

```bash
# 1) アカウントB（新環境）。AccountAId にアカウントAの12桁IDを渡す
aws cloudformation deploy \
  --template-file 02-account-b.yaml \
  --stack-name canary-verify-b \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides AccountAId=<アカウントAの12桁ID>

# Bの出力4つを取得（EndpointServiceName / PeerRoleArn / VpcId / FrontNewWebPrivateIp）
aws cloudformation describe-stacks --stack-name canary-verify-b \
  --query "Stacks[0].Outputs[].{Key:OutputKey,Value:OutputValue}" --output table

# 2) アカウントA（フロント/Canary）。Bの出力＋BのアカウントIDを渡す
aws cloudformation deploy \
  --template-file 01-account-a.yaml \
  --stack-name canary-verify-a \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    AllowedCidr=203.0.113.10/32 \
    EndpointServiceName=<Bの出力 EndpointServiceName> \
    AccountBId=<アカウントBの12桁ID> \
    PeerVpcId=<Bの出力 VpcId> \
    PeerRoleArn=<Bの出力 PeerRoleArn> \
    FrontNewWebPrivateIp=<Bの出力 FrontNewWebPrivateIp>
# → Peeringが作成され、Bのロール経由で自動承認。出力 PeeringConnectionId を控える

# 3) アカウントB を2回目更新（B→Aの戻りルート作成。フロントTG-newがhealthyになる）
aws cloudformation deploy \
  --template-file 02-account-b.yaml \
  --stack-name canary-verify-b \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides AccountAId=<アカウントAの12桁ID> \
    PeeringConnectionId=<Aの出力 PeeringConnectionId>
```

- `EndpointServiceName`／`PeerVpcId`等を空のままにすると、該当のクロスアカウントリソース（IFエンドポイント／Peering・ルート・TG-newのIPターゲット）は作成されない。アカB未払い出しの机上確認用。
- アカウントBの`AcceptanceRequired: false`＋許可プリンシパル（アカA）により、アカAのエンドポイントは自動で接続される。Peeringも`PeerRoleArn`経由でCFNが自動承認する。

## デプロイ後の手動作業

### 1. 新バックエンドのIPターゲット登録（必須）
アカウントAの`NewBackendTargetGroup`は空で作成される。インターフェースVPCエンドポイントのENIプライベートIPを手動登録する（CFNではENIのIPを自動解決できないため）。

```bash
# アカウントAで実行
EP=$(aws cloudformation describe-stacks --stack-name canary-verify-a \
  --query "Stacks[0].Outputs[?OutputKey=='ServiceEndpointId'].OutputValue" --output text)
TG=$(aws cloudformation describe-stacks --stack-name canary-verify-a \
  --query "Stacks[0].Outputs[?OutputKey=='NewBackendTargetGroupArn'].OutputValue" --output text)

# エンドポイントのENI → プライベートIPを取得
ENIS=$(aws ec2 describe-vpc-endpoints --vpc-endpoint-ids "$EP" \
  --query "VpcEndpoints[0].NetworkInterfaceIds" --output text)
IPS=$(aws ec2 describe-network-interfaces --network-interface-ids $ENIS \
  --query "NetworkInterfaces[].PrivateIpAddress" --output text)

# ターゲットグループへ登録
for ip in $IPS; do
  aws elbv2 register-targets --target-group-arn "$TG" \
    --targets Id=$ip,Port=80
done
```

### 2. Canaryの作成（コンソール）
1. API Gateway → `canary-verify-api` → prodステージ → Canaryタブ → Canary作成
2. リクエストの分配（%）を設定（最初は0%）
3. ステージ変数のCanaryオーバーライドを2つ設定：
   - `vpcLinkId` = 出力`CanaryVpcLinkId`（VPC Link②のID）。**これがルーティングの実体**（V1はconnectionIdだけで経路が決まる）。
   - `backendHost` = 出力`CanaryBackendHost`（NLB-newのDNS名。Hostヘッダー用）。
4. 「ステージキャッシュを使用」はオフのまま

### 3. フロント加重ルールのweight変更（コンソール）
`FrontWeightedRule`の重みを 旧100/新0 → 50/50 → 0/100 と変更しながら、フロントALBのレスポンスで振り分けを確認。

## 検証の入口と old/new の見分け方

各環境のnginxは、本文JSONに加えて**レスポンスヘッダー `X-Env: old|new`（＋ `X-Tier: front|backend`）**を返す。ヘッダーはAPI GW→VPC Link→ALB→PrivateLinkのチェーンを通り抜けるので、`curl -i` で中身を開かずに判別できる。

- フロント：出力`FrontAlbDnsName`に`http://`でアクセス → `X-Env: old|new` / `{"tier":"front","environment":"old|new",...}`
- バックエンド：出力`ApiInvokeUrl`にGET → `X-Env: old|new` / `{"tier":"backend","environment":"old|new",...}`（Canary比率に応じてold/newが混ざる）

```bash
curl -si "$ApiInvokeUrl/" | grep -i '^x-env:'   # 例: x-env: old
```

## パラメータ

### 01-account-a.yaml
| パラメータ | デフォルト | 用途 |
|---|---|---|
| `AllowedCidr` | `0.0.0.0/0` | フロントALB(80)とREST API invokeの送信元CIDR。検証クライアントIP(`x.x.x.x/32`)推奨 |
| `VpcCidr` | `10.0.0.0/16` | アカA検証VPCのCIDR（**BのCIDRと重複不可**） |
| `PublicSubnet1Cidr`/`2Cidr` | `10.0.0.0/24`/`10.0.1.0/24` | パブリックサブネット |
| `EndpointServiceName` | （空） | アカBの出力。空だとIFエンドポイント未作成 |
| `AccountBId` | （空） | アカウントBの12桁ID（Peeringの相手先） |
| `PeerVpcId` | （空） | アカBの出力`VpcId`。空だとPeering系リソース未作成 |
| `PeerRoleArn` | （空） | アカBの出力`PeerRoleArn`（Peering自動承認用） |
| `FrontNewWebPrivateIp` | （空） | アカBの出力`FrontNewWebPrivateIp`（フロントTG-newのIPターゲット） |
| `AccountBVpcCidr` | `10.1.0.0/16` | Peering経由のルート宛先（BのVpcCidrと一致させる） |
| `ContainerImage` | `public.ecr.aws/nginx/nginx:latest` | Fargateの公開イメージ |
| `NamePrefix` | `canary-verify` | リソース名プレフィックス（≤13文字、両スタックで揃える） |

### 02-account-b.yaml
| パラメータ | デフォルト | 用途 |
|---|---|---|
| `AccountAId` | （必須） | アカウントAの12桁ID（エンドポイントサービス許可先＋Peering承認ロールの信頼先） |
| `VpcCidr` | `10.1.0.0/16` | アカB検証VPCのCIDR（**AのCIDRと重複不可**＝Peeringの制約） |
| `PublicSubnet1Cidr`/`2Cidr` | `10.1.0.0/24`/`10.1.1.0/24` | パブリックサブネット |
| `AccountAVpcCidr` | `10.0.0.0/16` | フロントEC2のSG許可元＋戻りルート宛先（AのVpcCidrと一致させる） |
| `PeeringConnectionId` | （空） | **2回目更新で指定**（Aの出力`PeeringConnectionId`）。戻りルートを作成 |
| `ContainerImage` | `public.ecr.aws/nginx/nginx:latest` | Fargateの公開イメージ |
| `NamePrefix` | `canary-verify` | リソース名プレフィックス |

## 片付け

```bash
# アカウントA（IFエンドポイント削除でアカB側の接続も切れる）
aws cloudformation delete-stack --stack-name canary-verify-a
aws cloudformation wait stack-delete-complete --stack-name canary-verify-a
# アカウントB
aws cloudformation delete-stack --stack-name canary-verify-b
```

手動登録したターゲットはターゲットグループ削除時に消えるので個別の解除は不要。
