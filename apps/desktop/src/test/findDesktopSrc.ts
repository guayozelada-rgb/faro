// Ayuda de pruebas: localiza `apps/desktop/src` en disco. En jsdom `import.meta.url` no es una
// ruta de archivo, así que se busca hacia arriba desde el directorio de trabajo.
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";

const MARKER = join("styles", "tokens.css");

export function findDesktopSrc(start: string = process.cwd()): string {
  let dir = start;
  for (;;) {
    for (const candidate of [join(dir, "src"), join(dir, "apps", "desktop", "src")]) {
      if (existsSync(join(candidate, MARKER))) {
        return candidate;
      }
    }
    const parent = dirname(dir);
    if (parent === dir) {
      throw new Error("No se encontró apps/desktop/src");
    }
    dir = parent;
  }
}
