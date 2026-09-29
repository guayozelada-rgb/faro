import { describe, expect, it } from "vitest";

import { SECTIONS } from "@/app/sections";
import i18n, { LANGUAGES, NAMESPACES, resources } from "@/lib/i18n";
import { takeMissingKeys } from "@/test/setup";

const TODO_PREFIX = "[TODO] ";

/** Aplana un recurso a pares `clave.anidada → valor`. */
function flatten(value: unknown, prefix = ""): Map<string, unknown> {
  const entries = new Map<string, unknown>();
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    for (const [key, child] of Object.entries(value)) {
      for (const [path, leaf] of flatten(child, prefix ? `${prefix}.${key}` : key)) {
        entries.set(path, leaf);
      }
    }
  } else {
    entries.set(prefix, value);
  }
  return entries;
}

describe("recursos i18n", () => {
  it("tienen los 10 namespaces de la spec en los tres idiomas", () => {
    expect(NAMESPACES).toEqual([
      "common",
      "home",
      "inbox",
      "research",
      "content",
      "audit",
      "ads",
      "agents",
      "settings",
      "errors",
    ]);
    for (const lng of LANGUAGES) {
      expect(Object.keys(resources[lng]).sort()).toEqual([...NAMESPACES].sort());
    }
  });

  describe.each(NAMESPACES)("namespace %s", (ns) => {
    const es = flatten(resources.es[ns]);

    it("en español: todo valor es texto no vacío y traducido", () => {
      expect(es.size).toBeGreaterThan(0);
      for (const [key, value] of es) {
        expect(typeof value, `${ns}:${key}`).toBe("string");
        expect((value as string).trim(), `${ns}:${key}`).not.toBe("");
        expect(value as string, `${ns}:${key}`).not.toMatch(/^\[TODO\]/);
      }
    });

    it.each(["en", "pt-BR"] as const)("en %s: mismas claves que es, con [TODO] ", (lng) => {
      const other = flatten(resources[lng][ns]);
      expect([...other.keys()].sort()).toEqual([...es.keys()].sort());
      for (const [key, value] of other) {
        expect(typeof value, `${lng} ${ns}:${key}`).toBe("string");
        expect(value as string, `${lng} ${ns}:${key}`).toMatch(/^\[TODO\] \S/);
        expect(value, `${lng} ${ns}:${key}`).toBe(`${TODO_PREFIX}${String(es.get(key))}`);
      }
    });

    it("t() resuelve cada clave de es (no devuelve la clave)", () => {
      for (const key of es.keys()) {
        const text = i18n.t(key, { ns });
        expect(text, `${ns}:${key}`).toBe(es.get(key));
        expect(text).not.toBe(key);
      }
    });
  });

  it("cada sección tiene título y estado vacío", () => {
    for (const section of SECTIONS) {
      for (const key of ["title", "emptyState.title", "emptyState.description"]) {
        expect(i18n.exists(key, { ns: section.namespace }), `${section.namespace}:${key}`).toBe(
          true,
        );
      }
    }
  });

  it("la verificación de claves faltantes detecta una clave inexistente", () => {
    const text = i18n.t("emptyState.doesNotExist", { ns: "home" });
    expect(text).toBe("emptyState.doesNotExist");
    expect(takeMissingKeys()).toEqual(["home:emptyState.doesNotExist"]);
  });

  it("usa es como idioma fijo y de respaldo", () => {
    expect(i18n.language).toBe("es");
    expect(i18n.options.fallbackLng).toEqual(["es"]);
  });
});
