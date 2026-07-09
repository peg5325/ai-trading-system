# 익절 (ikjeol) — 설치 & 연동

## 개요

이 스킬은 **검수 기준만 담은 도구 중립 `SKILL.md`** 다. PR 읽기/코멘트 등록은 각 도구의
**GitHub 연동(MCP 또는 CLI)** 에 의존한다. 기준(두뇌)과 연동(손발)을 분리했기 때문에,
`SKILL.md` 는 Claude Code / Codex CLI / Gemini CLI 등 SKILL.md 오픈 표준을 따르는 어떤 도구에
넣어도 동일하게 동작한다.

- 대상 저장소: GitHub `peg5325/ai-trading-system`
- 검수 기조: **보고 후 승인 → 일반 코멘트 등록**, 머지 차단 안 함(판단은 텍스트로만)

## 설치 경로

폴더(`ikjeol/`) 통째로 아래 위치 중 하나에 두면 된다.

| 도구 | 전역 | 프로젝트 |
|---|---|---|
| **Claude Code** | `~/.claude/skills/ikjeol/` | `<repo>/.claude/skills/ikjeol/` |
| **Codex CLI** | `~/.codex/skills/ikjeol/` | `<repo>/.codex/skills/ikjeol/` |
| **Gemini CLI** | `~/.gemini/skills/ikjeol/` | `<repo>/.gemini/skills/ikjeol/` |

- 이 저장소에는 `skills/ikjeol/` 로 커밋되어 있다. 각 도구에서 쓰려면 위 경로로
  **복사하거나 심볼릭 링크**한다. 예:
  ```bash
  ln -s "$PWD/skills/ikjeol" ~/.claude/skills/ikjeol
  ```
- **Gemini CLI**: 최초 활성화 시 동의(consent) 프롬프트가 뜨며 스킬 폴더를 신뢰해야 한다.

## GitHub 연동

PR을 실제로 읽고 코멘트를 등록하려면 각 도구에 GitHub 접근을 붙여야 한다. `SKILL.md` 는 도구명을
포함하지 않으므로, 연동은 **도구별로 한 번** 설정한다.

- **Claude Code**: 이 저장소는 이미 `.mcp.json` 에 GitHub MCP(`https://api.githubcopilot.com/mcp/`)가
  구성되어 있다. 또는 `gh` CLI(로그인된 상태)로도 PR diff 조회·코멘트가 가능하다.
- **Codex CLI / Gemini CLI**: 각 도구의 MCP 설정에 동일 원리로 GitHub 접근을 주입한다.
  GitHub MCP 서버(또는 `gh` CLI)를 연결하고, PAT(Personal Access Token)는 **도구의 시크릿/헤더
  설정**에 넣는다.

> ⚠️ **토큰은 절대 `SKILL.md`·코드·커밋에 넣지 않는다.** MCP 헤더나 환경변수 등 도구의 시크릿
> 채널로만 주입한다. (현재 저장소 `.mcp.json` 헤더에 PAT가 평문으로 들어가 있는데, 별도로 회수·
> 재발급 후 로컬 전용 설정으로 옮기는 것을 권장 — 아래 "보안 주의" 참조.)

### 토큰 스코프 주의
- 연결 확인용으로 GitHub PR 목록/특정 PR 조회를 사용한다(예: `repos/peg5325/ai-trading-system/pulls`).
- PR 코멘트 등록에는 `pull_requests: write`(또는 fine-grained PAT의 해당 권한)가 필요하다.
  조회만 되고 코멘트가 실패하면 토큰 스코프를 먼저 확인한다.

## 검증

1. 각 도구에서 **새 세션**을 시작한다.
2. `PR 리뷰해줘` / `이 PR 검수해줘` 로 스킬이 활성화되는지 확인한다.
3. 활성화되지 않으면 점검: **설치 경로** 정확한지 / `SKILL.md` **frontmatter(name·description)** 의
   YAML 문법이 유효한지 / description에 트리거 문구가 들어 있는지.

## 보안 주의

- 이 저장소 `.mcp.json` 에 GitHub PAT가 평문으로 커밋되어 있다. 공개 저장소이므로 **해당 토큰은
  이미 노출된 것으로 간주하고 GitHub에서 즉시 폐기(revoke) 후 재발급**하고, 새 토큰은 커밋되지 않는
  로컬 설정(예: Claude Code `.claude/settings.local.json` env, 또는 셸 환경변수)에 두는 것을 권장한다.
- `.env`(TOSS 키), `status*.json`·`logs*/`·`reports/`·`data/`(계좌·시세)는 커밋 금지 대상이다.

## 도구 전환 주의 (Gemini)

무료 등급 Gemini CLI는 Antigravity CLI로 전환될 수 있다. `SKILL.md` 는 오픈 표준이라 후속 도구도
동일 형식을 이어받을 가능성이 높으나, 배포 전 각자 도구 버전·전환 여부를 확인하는 것을 권장한다.
