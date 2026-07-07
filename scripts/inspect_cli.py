"""조회(read-only) 인스펙션 CLI 진입점 — 실제 로직은 src/cli/inspect.py 에 있다.

주의: 파일명을 `inspect.py` 로 두면 안 된다. scripts/ 가 다른 스크립트 실행 시
sys.path[0] 로 들어가면서 표준 라이브러리 `inspect` 모듈을 shadow 해
(dataclasses 등이 내부적으로 `import inspect`) run_paper/run_reviews 등 모든 스크립트가
순환 임포트로 죽는다. 그래서 진입점은 `inspect_cli.py` 로 둔다.

사용:
  ./.venv/bin/python scripts/inspect_cli.py summary
  ./.venv/bin/python scripts/inspect_cli.py latest --limit 5
  ./.venv/bin/python scripts/inspect_cli.py metrics --last 24h
  ./.venv/bin/python scripts/inspect_cli.py errors --since today
  ./.venv/bin/python scripts/inspect_cli.py improvements --top 5
  ./.venv/bin/python scripts/inspect_cli.py system-review --latest
  # 동등: ./.venv/bin/python -m src.cli.inspect summary

읽기 전용 — 매매/주문/Runtime 제어와 무관. 라이브 Paper Trading 루프가 쓰는 중에도 안전.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.cli.inspect import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
