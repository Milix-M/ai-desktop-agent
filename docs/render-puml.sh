#!/usr/bin/env bash
# docs/images/*.png を docs/puml/*.puml から再生成する。
# 日本語表示のため Noto Sans JP をコンテナに取り込んで描画する。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PUML_DIR="$REPO_DIR/docs/puml"
IMG_DIR="$REPO_DIR/docs/images"

FONT_CANDIDATES=(
  "/mnt/c/Windows/Fonts/NotoSansJP-VF.ttf"
  "/usr/share/fonts/opentype/noto/NotoSansJP-Regular.otf"
  "/usr/share/fonts/truetype/noto/NotoSansJP-Regular.ttf"
)

FONT_SRC=""
for f in "${FONT_CANDIDATES[@]}"; do
  if [[ -f "$f" ]]; then
    FONT_SRC="$f"
    break
  fi
done

if [[ -z "$FONT_SRC" ]]; then
  echo "日本語フォントが見つかりません。Noto Sans JP をいずれかに配置してください:" >&2
  printf '  %s\n' "${FONT_CANDIDATES[@]}" >&2
  exit 1
fi

mkdir -p "$IMG_DIR"

# plantuml/plantuml は appuser 実行のため --user root でフォント導入する
docker run --rm --user root \
  -v "$PUML_DIR:/docs/puml:ro" \
  -v "$IMG_DIR:/docs/images" \
  -v "$FONT_SRC:/tmp/NotoSansJP.ttf:ro" \
  --entrypoint sh plantuml/plantuml:latest -c \
  'mkdir -p /usr/share/fonts/windows && cp /tmp/NotoSansJP.ttf /usr/share/fonts/windows/ && fc-cache -f >/dev/null 2>&1; java -jar /opt/plantuml.jar -tpng "/docs/puml/*.puml" -o /docs/images'

# Docker が root で書いた成果物の所有者を戻す
if [[ "$(id -u)" -ne 0 ]]; then
  sudo chown -R "$(id -u):$(id -g)" "$IMG_DIR" 2>/dev/null || true
fi

ls -l "$IMG_DIR"
