# platform/policies — NetworkPolicy 는 의도적으로 아직 없다 (2026-07-29)

Q6(Kafka 무인증 PLAINTEXT)의 짝은 "NetworkPolicy 접근 제어"지만, **지금 클러스터에는
default-deny 베이스라인 자체가 없다**(실측: netpol 은 argocd ns 의 자기방어 6개뿐 — app ns 도 무정책).
deny 없이 allow 만 깔면 아무 일도 안 하는 장식이 된다.

따라서 netpol 은 **default-deny 베이스라인 결정과 함께 별건**으로 간다. 그때 같이 정할 것:

- deny 모델(ingress-only vs ingress+egress) — egress deny 면 DNS·S3(barman)·`.8` 복제 허용이 전제
- kubelet probe 예외(노드 IP) — status §3 명시 함정
- 관측 스크레이프(observability → data 지표 포트) 허용
- pipeline/app → data 서비스 포트(5432·pg-pooler·9200·9092·6379) 허용
- `.8` 관련 허용의 제거 시점 = 런북 §4.1-①② (roll-forward 후)
