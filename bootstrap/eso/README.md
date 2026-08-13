# bootstrap/eso — ESO 스토어(비밀 백엔드)의 사이트 분기

> 신설 2026-08-10. 근거 = 앱 레포 `docs/mp_aws_prep_checklist.md` **0-2** · 결정 **C-23**(양 사이트 독립·번들 6) + **C-36**(백엔드 = Secrets Manager · C-23 의 SSM 을 정정).
> 구조 정본 = `SITES.md`.

## 한 줄

**온프렘 = `fb-kubernetes`(K8s provider, 현행 유지) / EKS = `mp-aws-secrets`(Secrets Manager + IRSA).**
`ExternalSecret` 쪽에서 갈리는 것은 **`secretStoreRef.name` 한 필드**이고,
**`remoteRef` 67엔트리는 한 글자도 안 바뀐다.**

## 🔴 이 디렉터리는 ArgoCD 가 읽지 않는다

`bootstrap/argocd` 와 같다. 두 뿌리(`mealplanning-root` · `platform-root`)의 감시 범위 **밖**이다.
`ClusterSecretStore` 는 클러스터 스코프이고 **ExternalSecret 이 존재하기 전에 이미 있어야** 하는
부트스트랩 오브젝트라, 워크로드 트랙과 같은 수명주기에 태우면 닭-달걀이 된다.

**적용자는 앱 레포 Ansible `k8s_eso` 롤**이다. 여기 있는 것은 그 롤이 만들어내야 할
**목표 상태의 기록**이며, 지금은 아직 정본이 아니다 — 정본을 Ansible 템플릿에 둘지
여기로 옮길지는 **미결(사람 결정)**. `bootstrap/argocd/README.md` 와 같은 미결이다.

## 왜 base 가 없나

다른 32개 트랙은 `base/ + overlays/{onprem,eks}` 인데 여기만 `overlays/` 만 있다.
**두 스토어가 공유하는 필드가 `apiVersion`·`kind` 말고 하나도 없기 때문**이다 —
`spec.provider` 아래가 `kubernetes:` 와 `aws:` 로 통째로 갈린다.
억지로 base 를 만들면 한쪽 overlay 가 base 의 provider 를 통째로 지우는 패치를 갖게 되고,
그건 "공통을 뽑아낸 것"이 아니라 **공통이 없다는 사실을 숨기는 것**이다.

부수 효과: `base/` 가 없으므로 `validate.py` 의 `check_site_overlays()`(base ↔ onprem 짝을 보는 검사)가
이 디렉터리를 스캔하지 않는다. 의도된 것이다.

## 매핑 — 온프렘 Secret 1개 ↔ Secrets Manager 시크릿 1개

"무수정"이 성립하는 이유가 이 1:1 대응이다. 스토어의 `prefix: mp/prod/` 가 `remoteRef.key` 앞에
붙어 시크릿 이름이 되고, `remoteRef.property` 는 그 JSON 값 안의 gjson 경로가 된다.

| `remoteRef.key` | 온프렘 = `fb-secrets` ns Secret | EKS = Secrets Manager 시크릿 | 엔트리 | 참조 ns |
|---|---|---|---|---|
| `app-secrets` | `app-secrets` | `mp/prod/app-secrets` | 33 | app · mp-ingress · observability |
| `data-secrets` | `data-secrets` | `mp/prod/data-secrets` | 14 | app · data · pipeline |
| `harbor-pull` | `harbor-pull` | `mp/prod/harbor-pull` | 15 | app · data · pipeline · mp-ingress · argo-rollouts |
| `pipeline-secrets` | `pipeline-secrets` | `mp/prod/pipeline-secrets` | 5 | pipeline |
| `alertmanager-slack` | `alertmanager-slack` | `mp/prod/alertmanager-slack` | 2 | observability — 🔴 **config 레포 밖** |
| `repo-food-budget-config` (온프렘) / `repo-mealplanning-config` (EKS) | `repo-food-budget-config` | `mp/prod/repo-mealplanning-config` | `dataFrom.extract` | argocd — 🔴 **config 레포 밖** |

= **번들 6**(C-23) × **백엔드 Secrets Manager**(C-36). 합계 67(config) + 2 + `extract` 1 = **70엔트리**,
체크리스트 수치와 일치한다.

⟳ **2026-08-13 정정** — 이 문서는 원래 백엔드를 "SSM standard" 로 적었다. **C-36 이 그것을 정정했고**
(2026-08-10 · 선생님 지시 · 근거 = 4KB 한도 소멸) 라이브 스토어도 `service: SecretsManager` 로 갔다.
🔴 **번들 6 은 유지된다** — 번들의 근거는 4KB 가 아니라 *"remoteRef 70엔트리 무수정 + 온프렘 Secret
과의 1:1"* 이고, 그 근거는 백엔드가 바뀌어도 그대로다. 🟢 오히려 4KB 천장이 사라져 **번들의 유일한
약점이 없어졌다**(아래 "값 적재" 참조).

🔴 **아래 두 줄은 이 레포에서 고칠 수 없다.** `mp-alertmanager-slack`(observability)·
`repo-food-budget-config`(argocd) ExternalSecret 은 config 레포에 없고 Ansible 이 직접 apply 한다.
**eks 에서 이 둘의 `secretStoreRef` 를 갈아주지 않으면 알림과 CD 자격증명이 조용히 NotReady 가 된다** —
파드는 뜨고 알림만 안 가고, ArgoCD 는 레포를 못 읽는다. → 아래 "앱 레포에 필요한 것" ③.

## 🔴 값 적재 — `remoteRef.property` 는 JSON 키다

Secrets Manager 시크릿 하나에 **JSON 오브젝트**를 넣는다. 키 이름 = 지금 `property` 에 쓰는 이름 그대로.

```jsonc
// mp/prod/app-secrets  (Secrets Manager — 값은 항상 KMS 로 암호화된다. 티어 개념이 없다)
{
  "PGPASSWORD": "…", "JWT_SECRET": "…",
  "KAKAO_CLIENT_ID": "…", "KAKAO_CLIENT_SECRET": "…",
  "GOOGLE_CLIENT_ID": "…", "GOOGLE_CLIENT_SECRET": "…",
  "CLOUDFLARE_API_TOKEN": "…", "GCP_SA_KEY_JSON": "{…}"   // ← 문자열로 이스케이프된 JSON
}
```

- **property 이름에 gjson 메타문자(`.` `*` `?` `#` `|` `@` `\`)가 0건**임을 실측했다(67/67).
  그래서 이스케이프가 필요 없고, 이것이 "무수정"의 두 번째 전제다. **새 키를 만들 때 이름에 `.` 을 넣지 말 것.**
- ⟳ **2026-08-13 — 4KB 천장이 사라졌다.** 원래 여기엔 *"standard tier = 4KB 상한 · `app-secrets`
  여유 711 B · advanced tier(8KB)는 파라미터당 과금"* 이라고 적혀 있었다. **C-36 이 백엔드를
  Secrets Manager 로 바꿨고 그쪽 상한은 64KB · 티어 개념이 없다.** 🟢 그래서 *"SA JSON 하나 더
  넣으면 초과"* 라는 위험이 없어졌고, 번들 6 의 유일한 약점이 사라졌다.
  🔴 **대신 값당 과금이 생겼다** — 시크릿 1개 = 월 $0.40. 번들 6 = $2.40, 키별로 풀면 $13.60.
  ⇒ 키를 늘릴 때 보는 것이 **바이트가 아니라 시크릿 개수**로 바뀌었다.
- `repo-mealplanning-config`(EKS) 는 **SSH 개인키** 다. JSON 문자열 안에서 개행이 `\n` 으로
  이스케이프된다. 🟢 실측 = **ed25519 432 B**(2026-08-13 · A0 에서 실제 적재·동기화 확인).
  RSA-4096 이었어도 64KB 라 무해하지만 ed25519 가 기본이다.

## IAM — 이 스토어가 요구하는 최소권한 (명세)

`prefix` 덕분에 Resource 를 경로 한정할 수 있다.

```jsonc
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
      "Resource": "arn:aws:secretsmanager:ap-northeast-2:<계정>:secret:mp/prod/*" }
  ]
}
```

🟢 **라이브 검증 완료 (2026-08-13 · A0)** — 앱 레포 `infra/terraform/aws-platform/iam_irsa.tf`
의 `aws_iam_role_policy.external_secrets`(`secrets-read`)가 이 형태이고, `mp/prod/repo-mealplanning-config`
가 `SecretSynced` 됐다.

- 🔴 **`Resource` 는 와일드카드여야 한다** — Secrets Manager 는 ARN 끝에 6자 랜덤 접미사를 붙인다
  (`secret:mp/prod/app-secrets-AbCdEf`). 이름을 정확히 박으면 **영원히 매치되지 않는다.**
- `secretsmanager:ListSecrets` 는 **넣지 않는다** — 우리는 `dataFrom.find` 를 한 곳도 안 쓴다(실측 0건).
  넣으면 "경로 아래 전부 나열"이 가능해져 `prefix` 로 얻은 경계가 약해진다.
  ⚠️ `dataFrom.extract`(argocd 배포키)는 키를 명시하므로 이 권한이 **필요 없다** — 실증됨.
- 🟢 **`kms:Decrypt` 는 필요 없었다** — 기본 키 `aws/secretsmanager` 로 만들면 IAM 에 KMS 문장이
  없어도 읽힌다(2026-08-13 실측). 🔴 **CMK 로 가면 달라진다**: `kms:Decrypt` + **A-26**(KMS 키
  정책에 이 롤 ARN 명시 — IAM 허용만으로는 거부)이 함께 필요하다. 키 선택은 **미결 ⑰**.
  🟢 되돌릴 수 있다 — `update-secret --kms-key-id`.
- 신뢰 주체 = ESO 컨트롤러 SA. IRSA 신뢰정책 Condition:
  `<OIDC>:sub = system:serviceaccount:external-secrets:external-secrets`.

## 🔴 앱 레포에 필요한 것 — 명세 (이 레포에서 할 수 없다)

Ansible `k8s_eso` 롤(`infra/ansible/roles/k8s_eso/`)은 **다른 레인 소관**이다. 넘길 것:

1. **`mp-aws-secrets` ClusterSecretStore 를 eks 사이트에서 apply** — 본문 = `overlays/eks/clustersecretstore.yaml`.
   온프렘에서는 apply 하지 않는다(C-23: 양 사이트 독립, 서로를 참조하지 않는다).
2. **ESO 컨트롤러 ServiceAccount 에 IRSA 애너테이션** —
   `eks.amazonaws.com/role-arn: arn:aws:iam::<계정>:role/<ESO 롤>`. Helm 차트라면 `serviceAccount.annotations`.
   🔴 이게 없으면 스토어는 `Ready=False`(NoCredentialProviders)로 뜨고 **ExternalSecret 30종이 전건 NotReady** 다.
3. **`mp-alertmanager-slack`·`repo-food-budget-config` 두 ExternalSecret 의 `secretStoreRef` 사이트 분기** —
   config 레포 밖이라 여기서 못 고친다. 위 "매핑" 표의 🔴 두 줄.
4. **온프렘 `fb-kubernetes` 는 그대로 둔다.** C-23 = 양 사이트 독립. PushSecret·자동복제 미채택.

## 미결 (사람 결정)

| # | 안건 | 왜 지금 못 정하나 |
|---|---|---|
| ⑤ | `spec.provider.aws.role` 로 스토어별 롤을 쓸지 | 계정 ID 미정(C-8 계정 3개 미생성). 안 쓰면 ESO 컨트롤러 롤 하나가 6번들 전체 권한을 갖는다 |
| ⑥ | SecureString 암호화 키 = AWS 관리 키 vs CMK | CMK 는 월 $1/키 + 감사·회전 이득. 예산 판정($857/월, 목표 $219) 안에서 볼 것 |
| ⑦ | `app-secrets` 4KB 초과 시 — advanced tier vs 번들 쪼개기 | 번들을 쪼개면 `remoteRef.key` 가 바뀌어 **"무수정"이 깨진다**. 초과 실측이 먼저 |
| ⑧ | 이 디렉터리 vs Ansible 템플릿 중 어디가 정본인가 | `bootstrap/argocd` 와 동일한 미결. 따로 정하면 둘이 갈린다 — **같이 정할 것** |
