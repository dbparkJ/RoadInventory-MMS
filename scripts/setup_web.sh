#!/usr/bin/env sh

set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
web_root="$project_root/webui"
built_index="$web_root/dist/index.html"
dry_run=false
for argument
do
    if [ "$argument" = "--dry-run" ]; then
        dry_run=true
        break
    fi
done

printf '%s\n' "[web-setup] Python/CUDA 환경을 구성합니다."
sh "$script_dir/setup.sh" "$@"

if [ "$dry_run" = true ]; then
    printf '%s\n' \
        "[web-setup] PLAN npm ci --no-audit --no-fund" \
        "[web-setup] PLAN npm run build"
    exit 0
fi

if ! command -v npm >/dev/null 2>&1; then
    if [ -f "$built_index" ]; then
        printf '%s\n' \
            "[web-setup] Node.js를 찾지 못했지만 빌드된 UI가 포함되어 있어 서버 실행에는 문제가 없습니다."
        exit 0
    fi
    printf '%s\n' \
        "Node.js/npm을 찾지 못했고 빌드된 UI도 없습니다." \
        "Node.js LTS를 설치한 뒤 이 스크립트를 다시 실행하십시오." >&2
    exit 2
fi

printf '%s\n' "[web-setup] 잠긴 프런트엔드 패키지를 설치합니다."
(cd "$web_root" && npm ci --no-audit --no-fund)
printf '%s\n' "[web-setup] 배포용 UI를 빌드합니다."
(cd "$web_root" && npm run build)
printf '%s\n' "[web-setup] 웹 작업실 설치가 완료되었습니다."
