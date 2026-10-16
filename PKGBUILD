# Local package build: run `makepkg -si` in this directory.
pkgname=tideline
pkgver=0.0.1
pkgrel=1
pkgdesc="Non-linear video editor built on MLT and GTK 4"
arch=('any')
url="https://tideline.video"
license=('GPL-3.0-or-later')
depends=('python' 'python-gobject' 'python-cairo' 'python-numpy' 'gtk4' 'libadwaita'
         'mlt' 'ffmpeg' 'frei0r-plugins' 'sdl2' 'qt6-base' 'qt6-svg' 'qt6-5compat')
optdepends=('rubberband: keep audio pitch when changing clip speed'
            'vid.stab: MLT stabilisation plugin'
            'rnnoise: MLT noise suppression plugin')
options=('!strip')

pkgver() {
  python -c "import re,io; print(re.search(r'VERSION = \"(.+?)\"', open('$startdir/tideline/__init__.py').read()).group(1))"
}

package() {
  cd "$startdir"
  local site
  site="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  install -d "$pkgdir$site"
  cp -r --no-preserve=ownership tideline "$pkgdir$site/"
  find "$pkgdir$site/tideline" -name '__pycache__' -type d -prune -exec rm -rf {} +
  python -m compileall -q -s "$pkgdir" -p / "$pkgdir$site/tideline"

  install -Dm755 packaging/tideline "$pkgdir/usr/bin/tideline"
  install -Dm644 packaging/org.tideline.Tideline.desktop \
    "$pkgdir/usr/share/applications/org.tideline.Tideline.desktop"
  local icon
  for icon in tideline/data/icons/hicolor/*/apps/*; do
    install -Dm644 "$icon" "$pkgdir/usr/share/icons/${icon#tideline/data/icons/}"
  done
  install -Dm644 packaging/org.tideline.Tideline.xml \
    "$pkgdir/usr/share/mime/packages/org.tideline.Tideline.xml"
  install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
}
