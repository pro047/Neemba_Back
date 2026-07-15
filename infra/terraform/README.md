# neemba AWS — terraform

AWS 자원은 여기서 테라폼으로 관리한다 (2026-07-15부터). state는
`s3://hymn-tfstate/neemba/terraform.tfstate` (계정 공용 tfstate 버킷, 프로젝트별 키).

```sh
cd infra/terraform
terraform init
terraform plan    # 변경 검토 — apply 전 사람이 확인
terraform apply
```

## 관리 중인 자원

- `neemba-db-backups-989785488374` S3 버킷 (+퍼블릭 차단, pg/ 90일 lifecycle)
- `neemba-ec2-role` IAM role + 인라인 정책(백업 버킷 pg/ 최소 권한)
- `neemba-ec2-profile` instance profile

## 테라폼 밖에 있는 것 (수동 관리)

- **EC2 인스턴스** (`i-067ebdc3337b14ff9`, EIP 13.125.26.93) — 손으로 만든
  프로덕션 인스턴스라 import 시 교체성 plan 위험이 있어 제외. 신규 인스턴스를
  만들 일이 생기면 그때 테라폼으로.
- **instance profile ↔ EC2 연결** — provider에 독립 리소스가 없음. 재연결 명령:

  ```sh
  aws ec2 associate-iam-instance-profile \
    --instance-id i-067ebdc3337b14ff9 \
    --iam-instance-profile Name=neemba-ec2-profile
  ```

- GHA deploy가 쓰는 IAM(secrets의 액세스 키), EC2 보안그룹 — 기존 수동 자원.
  테라폼으로 흡수할 후보이지만 아직 미착수.

## 규칙

- AWS 자원 변경은 이 디렉터리의 코드 수정 → `plan` 검토 → `apply` 순서로만.
  aws cli 직접 변경 금지 (drift 발생).
- `imports.tf`는 2026-07-15 손수 자원의 1회성 흡수 기록 — import apply가 끝난
  뒤에는 삭제해도 된다.
