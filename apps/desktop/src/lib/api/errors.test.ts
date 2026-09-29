import { describe, expect, it } from "vitest";

import { FaroError, getErrorMessage, isFaroErrorData, toFaroError } from "./errors";

const GENERIC = "Algo salió mal. Intenta de nuevo; si se repite, reinicia Faro.";

// Catálogo de errores de F0 (spec §5.4).
const CATALOG: [code: string, message: string][] = [
  ["engine.start_failed", "No pudimos iniciar el motor de Faro. Intenta de nuevo."],
  [
    "engine.restart_limit",
    "El motor se detuvo varias veces seguidas. Cierra Faro y vuelve a abrirlo.",
  ],
  ["engine.dev_unreachable", "No encontramos el motor de desarrollo en la dirección configurada."],
  ["engine.unauthorized", "El motor rechazó la conexión. Reinicia Faro."],
  ["engine.forbidden_host", "El motor rechazó la conexión. Reinicia Faro."],
  ["engine.not_found", "No encontramos lo que buscabas."],
  ["engine.invalid_request", "La solicitud no es válida. Intenta de nuevo."],
  ["engine.integrity_failed", "Faro no pudo verificar sus archivos. Reinstala la app."],
  [
    "vault.invalid_input",
    "Esa clave no tiene el formato esperado. Cópiala de nuevo desde la página del proveedor.",
  ],
  ["vault.invalid_key", "El proveedor rechazó esta clave. Revisa que esté completa y activa."],
  [
    "vault.key_restricted",
    "La clave funciona, pero no tiene los permisos que Faro necesita. Crea una clave con acceso completo.",
  ],
  [
    "vault.already_exists",
    "Ya tienes una clave de este proveedor. Reemplázala si quieres usar otra.",
  ],
  ["vault.not_found", "No encontramos esa clave. Puede que ya la hayas borrado."],
  [
    "vault.keyring_unavailable",
    "No pudimos abrir el llavero de tu computadora. Reinicia Faro e intenta de nuevo.",
  ],
  [
    "vault.provider_unreachable",
    "No pudimos comprobar la clave. Revisa tu conexión a internet e intenta de nuevo.",
  ],
  [
    "vault.provider_rate_limited",
    "El proveedor está recibiendo muchas solicitudes. Espera un minuto e intenta de nuevo.",
  ],
  ["vault.provider_error", "El proveedor tuvo un problema. Intenta de nuevo en unos minutos."],
  ["internal.unexpected", GENERIC],
];

describe("getErrorMessage", () => {
  it.each(CATALOG)("traduce %s con el catálogo en español", (code, message) => {
    expect(getErrorMessage(new FaroError(code, "respaldo del backend"))).toBe(message);
  });

  it("acepta el objeto tal como llega por IPC", () => {
    expect(getErrorMessage({ code: "vault.invalid_key", message: "", details: {} })).toBe(
      "El proveedor rechazó esta clave. Revisa que esté completa y activa.",
    );
  });

  it("usa el message del backend si el code no está en el catálogo (ADR 0002)", () => {
    expect(getErrorMessage(new FaroError("vault.new_code", "Mensaje del núcleo."))).toBe(
      "Mensaje del núcleo.",
    );
  });

  it.each([
    ["un code desconocido sin message", new FaroError("vault.new_code", "   ")],
    ["un code que apunta a un grupo del catálogo", new FaroError("vault", "")],
    ["un code con separador de namespace", new FaroError("common:appName", "")],
    ["un rechazo sin forma válida", "texto cualquiera"],
  ])("muestra el mensaje genérico para %s", (_label, error) => {
    expect(getErrorMessage(error)).toBe(GENERIC);
  });
});

describe("toFaroError", () => {
  it("devuelve la misma instancia si ya es FaroError", () => {
    const error = new FaroError("vault.not_found");
    expect(toFaroError(error)).toBe(error);
  });

  it("FaroError.unexpected tiene el code genérico y details vacío", () => {
    expect(FaroError.unexpected()).toMatchObject({
      code: "internal.unexpected",
      message: "",
      details: {},
      name: "FaroError",
    });
  });
});

describe("isFaroErrorData", () => {
  it("reconoce la forma común", () => {
    expect(isFaroErrorData({ code: "a.b", message: "", details: {} })).toBe(true);
  });

  it("rechaza formas incompletas", () => {
    expect(isFaroErrorData({ code: "a.b", message: "" })).toBe(false);
    expect(isFaroErrorData("a.b")).toBe(false);
  });
});
