# 検証用CloudFormationテンプレート（クロスアカウント版）

後編記事の検証環境。**アカウントA（フロント/Canary）**と**アカウントB（新環境）**の2アカウント・各1スタック構成。

## アーキテクチャ

```
アカウントA（01-account-a.yaml / 1スタック）
  (1) フロント検証：インターネット向けALB + 加重ターゲットグループ【クロスアカウント】
        ├ TG-old（instanceターゲット）→ EC2(old, アカA)
        └ TG-new（ipターゲット, AZ=all）→ ──VPC Peering──▶ EC2(new, アカB)
  (2) バックエンド検証：REST API + Canary（VPC Link V1 = 公式実証済みのCanary切替パターン）
        API GW（統合connectionId＝ステージ変数 vpcLinkId）
          ├ prod  : VPC Link①(V1) → NLB-old(A) ─ALB-typeTG→ ALB-old(A) → Fargate(old, アカA)
          └ canary: VPC Link②(V1) → NLB-new(A) ─IPターゲット=IFエンドポイントENI
                                                        │ PrivateLink
アカウントB（02-account-b.yaml / 1スタック）              ▼
  VPCエンドポイントサービス(NLB) → NLB(B) ──ALB-typeターゲット──▶ 内部ALB(B) → ECS Fargate(new)
  EC2(new, フロント用)  ← アカAのALBからPeering経由で到達
  Peering承認用IAMロール（アカAのCFNがassumeして自動承認）
```

- **フロントもクロスアカウント**：新側EC2はアカウントBに置き、アカウントAのALBから**VPC Peering＋IPターゲット**で振り分ける。バックエンドはPrivateLink——**同じクロスアカウントでも「Peering（CIDR非重複が必須）」と「PrivateLink（重複許容）」の2パターンを使い分ける**構成。
- Canaryは **`vpcLinkId`（統合のconnectionId）と`backendHost`（Hostヘッダー）の2つのステージ変数を上書き**して新旧経路を切替。V1プライベート統合では**ルーティングを決めるのはconnectionIdのみ**（uriはHostヘッダー用）。connectionIdのステージ変数化と`update-stage`での差し替えは公式ドキュメントに実例があり、全ホップが実績ある仕様で構成される。
- 旧側も新側も「NLB→（ALB→）Fargate」で、アプリ配信の基本形 ALB→Fargate は旧側=アカA内 / 新側=アカB内に維持。

## 重要な設計上の前提（公式ドキュメント＋実機確認の根拠）

- **REST APIのVPC Link V1はNLB専用**（`AWS::ApiGateway::VpcLink`の`TargetArns`はNLBのARN）。`connectionId`のステージ変数化は公式に明記（[set-up-api-with-vpclink-cli](https://docs.aws.amazon.com/apigateway/latest/developerguide/set-up-api-with-vpclink-cli.html)）。`uri`はルーティングには使われずHostヘッダー/証明書検証用（同上）。
- **V2（ALB直接統合）をCanary切替に使えない理由（実機確認済み）**：V2は統合に`IntegrationTarget`（ALBのARN）が必須で、未設定だと500 `Unable to find IntegrationTarget`。かつ **`IntegrationTarget`はステージ変数化不可**（`is not a valid ALB or NLB arn`で400）。つまりV2は固定ターゲット専用で、Canaryで統合先を切り替える軸には使えない。
- **NLBにALBをターゲット登録（ALB-type target group）できるのは同一アカウント・同一VPCのみ**。だからNLB(B)→ALB(B)はアカウントB内で完結させ、クロスアカウントはPrivateLinkで越える（[Use an ALB as a target of an NLB](https://docs.aws.amazon.com/elasticloadbalancing/latest/network/application-load-balancer-target.html)）。
- **NLBのIPターゲットにIFエンドポイントENIを登録**してPrivateLinkへ橋渡しするのは公式ブログのパターン（[Architecture patterns for consuming private APIs cross-account](https://aws.amazon.com/blogs/compute/architecture-patterns-for-consuming-private-apis-cross-account/)）。
- **VPC Link V1の作成には数分かかる**（公式は2〜4分と記載、実際はそれ以上のことも）。スタック作成/更新が長引いても待つ。V1にはV2のようなAZ制約はない。
- **フロント（Peering）側の設計根拠**：ALBの`instance`ターゲットは同一VPC限定 → 別VPC/別アカウントのEC2は**`ip`ターゲット**で登録する。LBのVPC外のIPターゲットは`AvailabilityZone: all`の指定が必須で、RFC1918のIPがPeering/TGW経由で到達可能なら登録できる（[Register targets](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/target-group-register-targets.html)）。クロスアカウントPeeringのCFN自動承認は、承認側（アカB）にIAMロールを作り要求側スタックの`PeerRoleArn`に渡す公式パターン（[AWS::EC2::VPCPeeringConnection](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-ec2-vpcpeeringconnection.html)）。
- **PeeringはCIDR重複不可**。A=10.0.0.0/16、B=10.1.0.0/16のデフォルトは非重複。変更する場合も重複させないこと（バックエンドのPrivateLinkだけなら重複可だが、フロントのPeeringが許容しない）。
- **B→Aの戻りルートはAスタック作成後にしか張れない**（pcx-IDが必要）ため、Bスタックを`PeeringConnectionId`パラメータ付きで**2回目更新**する3ステップ構成にしている。戻りルートができるまでフロントTG-newはunhealthy。

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

## 設計変遷と机上検証（2026-07-13）

- `cfn-lint 1.46.0` で両テンプレートともエラー・警告・情報レベルの指摘ゼロ（`--include-checks I` 込み）
- **設計変遷（実デプロイで判明した事実）**：
  1. 初版：V2 VPC Link＋ステージ変数でconnectionId切替 → 実行時500 `Unable to find IntegrationTarget`（**V2は統合に`IntegrationTarget`＝ALB ARNが必須**）
  2. 二版：`IntegrationTarget`をステージ変数化してCanary切替 → 変更セットが400 `is not a valid ALB or NLB arn`（**`IntegrationTarget`はステージ変数化不可**＝V2は固定ターゲット専用）
  3. 現行：**V1 VPC Link（NLB）×connectionIdステージ変数切替**。この切替は公式ドキュメントに実例がある確立されたパターンで、全ホップが実績ある仕様のみで構成される。
- **要デプロイ確認ポイント**：
  - VPC Link V1×2本の作成時間（各数分）。スタック更新が長くても待つ
  - Canaryで`vpcLinkId`を②に上書きした%のリクエストが、NLB-new→PrivateLink→アカBに届くか
  - NLB→ALB(ALB-typeターゲット)経由のヘルスチェックが通るか（アカA/BともALB SGはVPC CIDR許可）
  - フロント：Peering自動承認（`PeerRoleArn`）が通るか、B側2回目更新（戻りルート）後にTG-new（ipターゲット, AZ=all）がhealthyになるか
