#!/usr/bin/env bash
# First-AI-Agent Feedback Lab 로컬 개발 서버 실행 스크립트
#
# 설정된 환경 변수(HOST, PORT)를 참조하여 uvicorn 개발 서버를 실행합니다.
# 기본 바인딩: HOST=0.0.0.0, PORT=8001 (--reload 활성화)

set -euo pipefail

# 스크립트 위치 기준으로 프로젝트 루트 디렉터리 경로 계산
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")"/.. && pwd)"
cd "${project_root}"

# 가상환경 uvicorn 바이너리 존재 여부 확인
uvicorn_bin="${project_root}/.venv/bin/uvicorn"
if [[ ! -x "${uvicorn_bin}" ]]; then
    echo "[-] 오류: 가상환경 실행 파일(${uvicorn_bin})을 찾을 수 없습니다." >&2
    echo "    먼저 'uv venv .venv && uv pip install -e \".[dev]\"' 명령으로 환경을 구축해 주세요." >&2
    exit 1
fi

host="${HOST:-0.0.0.0}"
port="${PORT:-8001}"

echo "[*] Feedback Lab 서버를 시작합니다 (http://${host}:${port})..."
exec "${uvicorn_bin}" \
    app.main:app \
    --host "${host}" \
    --port "${port}" \
    --reload