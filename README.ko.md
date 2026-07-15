# antigravity-agy

[English](README.md) · [한국어](README.ko.md)

**Claude Code**에서 **Google Antigravity**(`agy` CLI)를 오케스트레이션하는 플러그인 —
OpenAI `codex` 플러그인의 `agy` 버전. Antigravity가 노출하는 Gemini / Claude / GPT
모델로 코딩 작업 위임, 세컨드 오피니언, 코드 리뷰를 수행한다. OAuth 연동 없음:
각 CLI가 자체 인증을 유지하고 Claude는 프로세스만 띄운다.

## 요구사항

- [`agy`](https://antigravity.google) CLI 설치 **+ 로그인**
- Node.js 18+

## 설치

```
/plugin marketplace add KyongSik-Yoon/antigravity-agy
/plugin install agy@antigravity-agy
```

## 커맨드

| 커맨드 | 기능 |
|--------|------|
| `/agy:rescue [작업]` | 코딩/진단 작업을 agy에 위임 (`agy-rescue` 서브에이전트 경유) |
| `/agy:review` | 현재 git diff 리뷰 |
| `/agy:adversarial-review [초점]` | 현재 diff에서 버그/보안 취약점 사냥 |
| `/agy:status [--all]` | 백그라운드 job 상태 |
| `/agy:result [job-id]` | 완료된 백그라운드 job 결과 회수 |
| `/agy:config [set-model "<이름>"]` | 기본 모델 조회/저장 |
| `/agy:hint` | 치트시트: 현재 모델, 가능 모델, 커맨드 |

## 모델 선택

기본 모델: **`Gemini 3.5 Flash (High)`**.

단발: `--model "<이름>"` (`agy models` 출력 정확 문자열, 예: `"Gemini 3.1 Pro (High)"`).
`task`에 전달한 `--model`은 새 기본값으로 **저장**된다.

영속 기본값(`~/.claude/agy/config.json`):

```
/agy:config set-model "Gemini 3.1 Pro (High)"
/agy:config                 # 조회
/agy:config clear-model     # Gemini 3.5 Flash (High)로 복귀
```

우선순위: `--model` 플래그 > `AGY_MODEL` 환경변수 > 저장된 config > 내장 기본값.
`task`, `review`에 적용.

## 권한 모델 (안전 기본값)

- 기본 = **읽기전용** (`--mode plan`)
- `--write` = 편집 허용, 툴마다 프롬프트 (`--mode accept-edits`)
- `--yolo` = `--dangerously-skip-permissions`를 추가하는 유일한 플래그

## 동작 원리

`scripts/agy-companion.mjs`가 `agy -p`(헤드리스)를 감싼다. `agy`엔 네이티브
백그라운드/job 저장소가 없어서 companion이 `~/.claude/agy/jobs/` 아래 파일 기반
저장소를 얹는다. `review`는 `git diff`를 agy에 넣고 findings를 요청한다.

## 라이선스

MIT
