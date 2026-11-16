"""실행 위치와 무관하게 프로젝트의 data/와 .env 위치를 찾습니다."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
