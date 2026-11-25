# The viewer's three.js bundle

`src/pcbfab2step/web/vendor/three.bundle.min.js` is three.js with the addons
the viewer uses, as one ES module, so the viewer runs offline and from a
single file (`--view` without pywebview embeds it in the page). To rebuild it
for a new three.js release (Node.js 18 or newer):

```
mkdir build && cd build
npm init -y
npm install three esbuild
cat > entry.js <<'JS'
export * from "three";
export { OrbitControls } from "three/addons/controls/OrbitControls.js";
export { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";
JS
npx esbuild entry.js --bundle --format=esm --minify --legal-comments=eof --target=es2020 \
    --outfile=three.bundle.min.js
```

Put a comment line with the three.js version on top, copy it to
`src/pcbfab2step/web/vendor/` with `node_modules/three/LICENSE` as
`LICENSE-three.txt`, and check the viewer (`pcbfab2step FAB --view`).
The bundle in the repository is three.js r187.
