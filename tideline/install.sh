#!/bin/sh
# Install Tideline for the current user without a package manager:
# the `tideline` command, menu entry, icons and the .tideline file type.
#
#   ./install.sh              install into ~/.local
#   ./install.sh --uninstall  remove it again
#   PREFIX=/usr/local sudo -E ./install.sh   system-wide
#
# On Arch, `makepkg -si` is the better route. The dependencies (GTK 4, MLT,
# FFmpeg, PyGObject…) come from your distribution; see README.md.
set -eu

SRC="$(dirname "$(readlink -f "$0")")"
PREFIX="${PREFIX:-$HOME/.local}"
APP_ID=org.tideline.Tideline
LIB="$PREFIX/lib/tideline"
SHARE="$PREFIX/share"

uninstall() {
    rm -rf "$LIB"
    rm -f "$PREFIX/bin/tideline" "$SHARE/applications/$APP_ID.desktop" "$SHARE/mime/packages/$APP_ID.xml"
    find "$SHARE/icons/hicolor" -name "$APP_ID.*" -delete 2>/dev/null || true
}

refresh() {
    command -v update-desktop-database >/dev/null && update-desktop-database -q "$SHARE/applications" || true
    command -v update-mime-database >/dev/null && update-mime-database "$SHARE/mime" || true
    command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t -f "$SHARE/icons/hicolor" || true
}

if [ "${1:-}" = "--uninstall" ]; then
    uninstall
    refresh
    echo "Tideline removed from $PREFIX"
    exit 0
fi

uninstall
mkdir -p "$LIB" "$PREFIX/bin"
cp -r "$SRC/tideline" "$LIB/"
find "$LIB" -name '__pycache__' -type d -prune -exec rm -rf {} +

cat > "$PREFIX/bin/tideline" <<EOF
#!/bin/sh
PYTHONPATH="$LIB\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m tideline "\$@"
EOF
chmod 755 "$PREFIX/bin/tideline"

install -Dm644 "$SRC/packaging/$APP_ID.desktop" "$SHARE/applications/$APP_ID.desktop"
sed -i "s|^Exec=tideline|Exec=$PREFIX/bin/tideline|" "$SHARE/applications/$APP_ID.desktop"
install -Dm644 "$SRC/packaging/$APP_ID.xml" "$SHARE/mime/packages/$APP_ID.xml"
for icon in "$SRC"/tideline/data/icons/hicolor/*/apps/*; do
    rel="${icon#"$SRC"/tideline/data/icons/}"
    install -Dm644 "$icon" "$SHARE/icons/$rel"
done
refresh

echo "Tideline installed into $PREFIX"
case ":$PATH:" in
    *":$PREFIX/bin:"*) ;;
    *) echo "Note: $PREFIX/bin is not on your PATH; start it from the app menu or add it." ;;
esac
