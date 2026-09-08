#!/usr/bin/env sh

set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
web_root="$project_root/webui"
venv_dir=.venv
dry_run=false
expect_value=
for argument
do
    case "$expect_value" in
        venv) venv_dir=$argument; expect_value=; continue ;;
        project) project_root=$argument; expect_value=; continue ;;
    esac
    case "$argument" in
        --dry-run) dry_run=true ;;
        --venv-dir) expect_value=venv ;;
        --venv-dir=*) venv_dir=${argument#--venv-dir=} ;;
        --project-root) expect_value=project ;;
        --project-root=*) project_root=${argument#--project-root=} ;;
    esac
done
project_root=$(CDPATH= cd -- "$project_root" && pwd)
web_root="$project_root/webui"
case "$venv_dir" in
    /*) venv_python="$venv_dir/bin/python" ;;
    *) venv_python="$project_root/$venv_dir/bin/python" ;;
esac

printf '%s\n' "[web-setup] Python/CUDA 환경을 구성합니다."
sh "$script_dir/setup.sh" "$@"

if [ "$dry_run" = true ]; then
    printf '%s\n' \
        "[web-setup] PLAN npm ci --no-audit --no-fund" \
        "[web-setup] PLAN npm run build" \
        "[web-setup] PLAN Python scripts/build_web.py verify (also required without npm)"
    exit 0
fi

if ! command -v npm >/dev/null 2>&1; then
    printf '%s\n' "[web-setup] npm unavailable; verifying packaged source and UI assets."
    "$venv_python" "$script_dir/build_web.py" verify --project-root "$project_root"
    exit $?
fi

printf '%s\n' "[web-setup] 잠긴 프런트엔드 패키지를 설치합니다."
(cd "$web_root" && npm ci --no-audit --no-fund)
printf '%s\n' "[web-setup] 배포용 UI를 빌드합니다."
(cd "$web_root" && MMS_BUILD_PYTHON="$venv_python" npm run build)
"$venv_python" "$script_dir/build_web.py" verify --project-root "$project_root"
printf '%s\n' "[web-setup] 웹 작업실 설치가 완료되었습니다."
