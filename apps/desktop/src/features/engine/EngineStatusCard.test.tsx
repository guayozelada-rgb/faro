import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { AppProviders } from "@/App";
import type { EngineStatus, FaroErrorData } from "@/lib/api/types";
import { deferred, mockEngineIpc } from "@/test/engineIpc";

import { EngineStatusCard } from "./EngineStatusCard";

// Textos exactos de la spec §3.2 y §5.4.
const STARTING = "Encendiendo el motor de Faro…";
const READY = "Motor conectado";
const RESTARTING = "El motor no responde. Estamos intentando reconectarlo.";
const RETRY = "Reintentar conexión";
const TITLE_TOOLTIP = "El motor es la parte de Faro que hace el trabajo en tu computadora.";
const START_FAILED = "No pudimos iniciar el motor de Faro. Intenta de nuevo.";
const RESTART_LIMIT = "El motor se detuvo varias veces seguidas. Cierra Faro y vuelve a abrirlo.";
const UNEXPECTED = "Algo salió mal. Intenta de nuevo; si se repite, reinicia Faro.";

const starting: EngineStatus = { state: "starting", version: null, error: null };
const ready: EngineStatus = { state: "ready", version: "0.1.0", error: null };
const restarting: EngineStatus = { state: "restarting", version: null, error: null };

function engineError(code: string, message = "Mensaje del núcleo."): EngineStatus {
  const error: FaroErrorData = { code, message, details: {} };
  return { state: "error", version: null, error };
}

/** Manejador de IPC que rechaza con cualquier valor, como haría el núcleo o Tauri. */
function rejectWith(value: unknown) {
  return () => {
    throw value;
  };
}

function renderCard() {
  return render(
    <AppProviders>
      <EngineStatusCard />
    </AppProviders>,
  );
}

function getCard(): HTMLElement {
  return screen.getByRole("region", { name: "Motor de Faro" });
}

function getStatusLine(): HTMLElement {
  return within(getCard()).getByRole("status");
}

async function expectStatus(text: string): Promise<void> {
  await waitFor(() => {
    expect(getStatusLine()).toHaveTextContent(text);
  });
}

describe("EngineStatusCard", () => {
  it("starting: esqueleto y 'Encendiendo el motor de Faro…', sin botón", async () => {
    const ipc = mockEngineIpc({ status: () => starting });
    renderCard();

    await waitFor(() => {
      expect(ipc.calls).toContain("engine_status");
    });
    await expectStatus(STARTING);
    expect(getCard()).toHaveAttribute("aria-busy", "true");
    expect(getCard().querySelector('[data-slot="skeleton"]')).not.toBeNull();
    expect(within(getCard()).queryByRole("button", { name: RETRY })).not.toBeInTheDocument();
  });

  it("mientras lee el estado por primera vez muestra el esqueleto de 'starting'", () => {
    mockEngineIpc({ status: () => new Promise<never>(() => undefined) });
    renderCard();

    expect(getStatusLine()).toHaveTextContent(STARTING);
    expect(getCard()).toHaveAttribute("data-state", "starting");
  });

  it("ready: 'Motor conectado' con la versión en un tooltip", async () => {
    const user = userEvent.setup();
    mockEngineIpc({ status: () => ready });
    renderCard();

    await expectStatus(READY);
    expect(getCard()).toHaveAttribute("aria-busy", "false");
    expect(getCard().querySelector('[data-slot="skeleton"]')).toBeNull();
    expect(within(getCard()).queryByRole("button", { name: RETRY })).not.toBeInTheDocument();
    // La versión no se ve hasta abrir el tooltip.
    expect(screen.queryByText("Versión 0.1.0")).not.toBeInTheDocument();

    await user.hover(screen.getByRole("button", { name: "Ver la versión del motor" }));

    const tooltips = await screen.findAllByRole("tooltip");
    expect(tooltips.some((tooltip) => tooltip.textContent === "Versión 0.1.0")).toBe(true);
  });

  it("ready sin versión no muestra el botón de versión", async () => {
    mockEngineIpc({ status: () => ({ ...ready, version: null }) });
    renderCard();

    await expectStatus(READY);
    expect(
      screen.queryByRole("button", { name: "Ver la versión del motor" }),
    ).not.toBeInTheDocument();
  });

  it("restarting: aviso sin botón", async () => {
    mockEngineIpc({ status: () => restarting });
    renderCard();

    await expectStatus(RESTARTING);
    expect(getCard()).toHaveAttribute("data-state", "restarting");
    expect(within(getCard()).queryByRole("button", { name: RETRY })).not.toBeInTheDocument();
  });

  it.each([
    ["engine.start_failed", START_FAILED],
    ["engine.restart_limit", RESTART_LIMIT],
  ])("error %s: mensaje traducido y botón Reintentar conexión", async (code, text) => {
    mockEngineIpc({ status: () => engineError(code) });
    renderCard();

    await expectStatus(text);
    expect(getStatusLine()).not.toHaveTextContent("Mensaje del núcleo.");
    expect(getStatusLine()).not.toHaveTextContent(code);
    expect(within(getCard()).getByRole("button", { name: RETRY })).toBeEnabled();
  });

  it("error con un code fuera del catálogo muestra el mensaje del núcleo", async () => {
    mockEngineIpc({ status: () => engineError("engine.algo_nuevo", "El motor tuvo un problema.") });
    renderCard();

    await expectStatus("El motor tuvo un problema.");
  });

  it("error sin code conocido ni mensaje muestra el mensaje genérico", async () => {
    mockEngineIpc({ status: () => engineError("engine.algo_nuevo", "") });
    renderCard();

    await expectStatus(UNEXPECTED);
  });

  it("error sin objeto de error muestra el mensaje genérico", async () => {
    mockEngineIpc({ status: () => ({ state: "error", version: null, error: null }) });
    renderCard();

    await expectStatus(UNEXPECTED);
  });

  it("Reintentar conexión invoca engine_restart y muestra el estado devuelto", async () => {
    const user = userEvent.setup();
    const restart = deferred<EngineStatus>();
    const ipc = mockEngineIpc({
      status: () => engineError("engine.start_failed"),
      restart: () => restart.promise,
    });
    renderCard();
    await expectStatus(START_FAILED);

    await user.click(within(getCard()).getByRole("button", { name: RETRY }));

    expect(ipc.calls.filter((cmd) => cmd === "engine_restart")).toHaveLength(1);
    const busyButton = await within(getCard()).findByRole("button", {
      name: "Reintentando conexión…",
    });
    expect(busyButton).toBeDisabled();

    await act(async () => {
      restart.resolve(starting);
      await restart.promise;
    });

    await expectStatus(STARTING);
    expect(within(getCard()).queryByRole("button")).toHaveAccessibleName("Qué es el motor");
  });

  it("Reintentar conexión funciona con el teclado", async () => {
    const user = userEvent.setup();
    const ipc = mockEngineIpc({
      status: () => engineError("engine.start_failed"),
      restart: () => starting,
    });
    renderCard();
    await expectStatus(START_FAILED);

    within(getCard()).getByRole("button", { name: RETRY }).focus();
    await user.keyboard("{Enter}");

    await expectStatus(STARTING);
    expect(ipc.calls).toContain("engine_restart");
  });

  it("si engine_restart falla, muestra ese error traducido", async () => {
    const user = userEvent.setup();
    mockEngineIpc({
      status: () => engineError("engine.start_failed"),
      restart: rejectWith({ code: "engine.restart_limit", message: "", details: {} }),
    });
    renderCard();
    await expectStatus(START_FAILED);

    await user.click(within(getCard()).getByRole("button", { name: RETRY }));

    await expectStatus(RESTART_LIMIT);
    expect(within(getCard()).getByRole("button", { name: RETRY })).toBeEnabled();
  });

  it("si engine_status falla, muestra el error genérico y permite reintentar", async () => {
    const user = userEvent.setup();
    mockEngineIpc({
      status: rejectWith(new Error("fallo del IPC")),
      restart: () => ready,
    });
    renderCard();

    await expectStatus(UNEXPECTED);

    await user.click(within(getCard()).getByRole("button", { name: RETRY }));

    await expectStatus(READY);
  });

  it("reacciona a los eventos engine://status", async () => {
    const ipc = mockEngineIpc({ status: () => starting });
    renderCard();
    await expectStatus(STARTING);
    await waitFor(() => {
      expect(ipc.activeListeners()).toBe(1);
    });

    ipc.emitStatus(ready);
    await expectStatus(READY);

    ipc.emitStatus(restarting);
    await expectStatus(RESTARTING);

    ipc.emitStatus(engineError("engine.restart_limit"));
    await expectStatus(RESTART_LIMIT);
    expect(within(getCard()).getByRole("button", { name: RETRY })).toBeInTheDocument();

    ipc.emitStatus(ready);
    await expectStatus(READY);
    expect(within(getCard()).queryByRole("button", { name: RETRY })).not.toBeInTheDocument();
  });

  it("ignora eventos con una carga que no es EngineStatus", async () => {
    const ipc = mockEngineIpc({ status: () => ready });
    renderCard();
    await expectStatus(READY);
    await waitFor(() => {
      expect(ipc.activeListeners()).toBe(1);
    });

    ipc.emitStatus({ state: "roto", version: null, error: null });
    ipc.emitStatus({ state: "error", version: null, error: "texto" });

    await expectStatus(READY);
  });

  it("un evento que llega antes de la primera lectura no queda pisado por ella", async () => {
    const firstRead = deferred<EngineStatus>();
    const ipc = mockEngineIpc({ status: () => firstRead.promise });
    renderCard();
    await waitFor(() => {
      expect(ipc.activeListeners()).toBe(1);
    });

    ipc.emitStatus(ready);
    await expectStatus(READY);

    await act(async () => {
      firstRead.resolve(starting);
      await firstRead.promise;
    });

    await expectStatus(READY);
  });

  it("después de un error de engine_restart, un evento nuevo manda sobre ese error", async () => {
    const user = userEvent.setup();
    const ipc = mockEngineIpc({
      status: () => engineError("engine.start_failed"),
      restart: rejectWith({ code: "engine.restart_limit", message: "", details: {} }),
    });
    renderCard();
    await expectStatus(START_FAILED);
    await waitFor(() => {
      expect(ipc.activeListeners()).toBe(1);
    });
    await user.click(within(getCard()).getByRole("button", { name: RETRY }));
    await expectStatus(RESTART_LIMIT);

    ipc.emitStatus(engineError("engine.dev_unreachable"));

    await expectStatus("No encontramos el motor de desarrollo en la dirección configurada.");
  });

  it("deja de escuchar el evento al desmontarse", async () => {
    const ipc = mockEngineIpc({ status: () => ready });
    const { unmount } = renderCard();
    await waitFor(() => {
      expect(ipc.activeListeners()).toBe(1);
    });

    unmount();

    await waitFor(() => {
      expect(ipc.activeListeners()).toBe(0);
    });
    expect(ipc.calls).toContain("plugin:event|unlisten");
  });

  it("si se desmonta antes de terminar la suscripción, igual deja de escuchar", async () => {
    const ipc = mockEngineIpc({ status: () => ready });
    const { unmount } = renderCard();

    unmount();

    await waitFor(() => {
      expect(ipc.calls).toContain("plugin:event|unlisten");
    });
    expect(ipc.activeListeners()).toBe(0);
  });

  it("el título tiene un tooltip que explica qué es el motor", async () => {
    const user = userEvent.setup();
    mockEngineIpc({ status: () => ready });
    renderCard();

    await user.tab();
    expect(screen.getByRole("button", { name: "Qué es el motor" })).toHaveFocus();

    const tooltips = await screen.findAllByRole("tooltip");
    expect(tooltips.some((tooltip) => tooltip.textContent === TITLE_TOOLTIP)).toBe(true);
  });

  it("anuncia el estado en una región viva educada que no cambia entre estados", async () => {
    const ipc = mockEngineIpc({ status: () => starting });
    renderCard();
    const statusLine = getStatusLine();
    expect(statusLine).toHaveAttribute("aria-live", "polite");
    expect(statusLine).toHaveAttribute("aria-atomic", "true");
    await waitFor(() => {
      expect(ipc.activeListeners()).toBe(1);
    });

    ipc.emitStatus(ready);
    await expectStatus(READY);

    expect(getStatusLine()).toBe(statusLine);
  });
});
