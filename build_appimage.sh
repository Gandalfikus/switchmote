#!/usr/bin/env bash
set -e

APP_NAME="Switchmote"
MAIN_SCRIPT="switchmote.py"
BUILD_DIR="build_env"
APP_DIR="AppDir"

echo "=== 1. Vorbereitungen & Abhängigkeiten prüfen ==="
if ! command -v appimagetool &> /dev/null; then
    echo "Lade appimagetool herunter..."
    curl -L -o appimagetool https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x appimagetool
    APPIMAGETOOL="./appimagetool"
else
    APPIMAGETOOL="appimagetool"
fi

echo "=== 2. Erstelle temporäre Python Build-Umgebung ==="
# Altes Build-Verzeichnis löschen, um sauberen Stand zu erzwingen
rm -rf "$BUILD_DIR" "$APP_DIR" dist build *.spec

# --system-site-packages stellt sicher, dass 'tk' / 'tkinter' aus pacman erkannt wird
python3 -m venv --system-site-packages "$BUILD_DIR"
source "$BUILD_DIR/bin/activate"

pip install --upgrade pip
pip install evdev pyinstaller

echo "=== 3. Kompiliere Python-Skript inkl. evdev & tkinter ==="
pyinstaller --noconfirm --onedir --windowed \
    --name "$APP_NAME" \
    --collect-all evdev \
    --collect-all tkinter \
    "$MAIN_SCRIPT"

deactivate

echo "=== 4. Erstelle AppDir-Struktur ==="
mkdir -p "$APP_DIR/usr/bin"
mkdir -p "$APP_DIR/usr/share/icons/hicolor/256x256/apps"

cp -r "dist/$APP_NAME/"* "$APP_DIR/usr/bin/"

cat << 'EOF' > "$APP_DIR/AppRun"
#!/bin/sh
HERE="$(dirname "$(readlink -f "${0}")")"
export PATH="${HERE}/usr/bin:${PATH}"
export LD_LIBRARY_PATH="${HERE}/usr/bin:${LD_LIBRARY_PATH}"
exec "${HERE}/usr/bin/Switchmote" "$@"
EOF
chmod +x "$APP_DIR/AppRun"

cat << EOF > "$APP_DIR/$APP_NAME.desktop"
[Desktop Entry]
Name=$APP_NAME
Exec=Switchmote
Icon=switchmote
Type=Application
Categories=Utility;Game;
Terminal=false
Comment=Bluetooth Controller Manager & Xbox-Mapper
EOF

convert -size 256x256 xc:transparent "$APP_DIR/switchmote.png" 2>/dev/null || \
  echo "" > "$APP_DIR/switchmote.png"
cp "$APP_DIR/switchmote.png" "$APP_DIR/usr/share/icons/hicolor/256x256/apps/switchmote.png"

echo "=== 5. Baue AppImage ==="
ARCH=x86_64 $APPIMAGETOOL "$APP_DIR" "Switchmote-x86_64.AppImage"

echo "=== Fertig! Switchmote-x86_64.AppImage erfolgreich erstellt. ==="