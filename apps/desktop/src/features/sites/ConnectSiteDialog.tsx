import { useQueryClient } from "@tanstack/react-query";
import { CircleCheck, CircleX, Download, LoaderCircle, Lock } from "lucide-react";
import { type SubmitEvent, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { toast } from "@/components/ui/sonner";
import { FaroError, getErrorMessage, toFaroError } from "@/lib/api/errors";
import {
  connectSite,
  normalizePairingCode,
  reconnectSite,
  siteDisplayName,
  type SiteOut,
} from "@/lib/api/sites";
import { exportWpPlugin } from "@/lib/api/wpPlugin";

import { markSiteVerified } from "./siteActivity";
import { markSiteCheckedThisSession } from "./useAutoCheckSites";
import { refreshSites, upsertSite } from "./useSites";

const TOTAL_STEPS = 3;

type Step = 1 | 2 | 3;

/** Errores que se muestran bajo el campo de la dirección (spec F1a §3.3). */
const URL_ERRORS = new Set([
  "site.invalid_url",
  "site.https_required",
  "site.address_not_allowed",
  "site.unreachable",
  "site.tls_error",
  "site.plugin_not_found",
  "site.moved",
  "site.blocked",
]);

/** Errores que se muestran bajo el campo del código. */
const CODE_ERRORS = new Set([
  "site.invalid_code_format",
  "site.pairing_code_invalid",
  "site.pairing_code_expired",
  "site.rate_limited",
]);

type Field = "url" | "code" | "form";

interface FieldError {
  field: Field;
  message: string;
  /** Solo con `site.already_connected`: el sitio que ya está en Faro. */
  existingSiteId?: string;
}

export type ConnectSiteMode = { kind: "connect" } | { kind: "reconnect"; site: SiteOut };

export interface ConnectSiteDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mode: ConnectSiteMode;
  /** "Ir al sitio": cierra el diálogo y enfoca la tarjeta de ese sitio. */
  onGoToSite: (siteId: string) => void;
}

/**
 * Asistente "Conectar tu sitio" en tres pasos; también sirve para volver a conectar
 * (empieza en el paso 2 con la dirección fija). El contenido solo se monta mientras está
 * abierto: al cerrarse, el código de vinculación (que solo vive en el estado del campo) se
 * descarta. El código nunca pasa por `useMutation`, la caché de Query ni el almacenamiento.
 */
export function ConnectSiteDialog({
  open,
  onOpenChange,
  mode,
  onGoToSite,
}: ConnectSiteDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {open ? (
        <ConnectSiteDialogContent
          mode={mode}
          onClose={() => {
            onOpenChange(false);
          }}
          onGoToSite={onGoToSite}
        />
      ) : null}
    </Dialog>
  );
}

interface ContentProps {
  mode: ConnectSiteMode;
  onClose: () => void;
  onGoToSite: (siteId: string) => void;
}

type ExportState =
  | { status: "idle" }
  | { status: "saving" }
  | { status: "done"; fileName: string }
  | { status: "error"; error: FaroError };

function ConnectSiteDialogContent({ mode, onClose, onGoToSite }: ContentProps) {
  const { t, i18n } = useTranslation("settings");
  const queryClient = useQueryClient();
  const reconnecting = mode.kind === "reconnect";

  const stepHeadingRef = useRef<HTMLHeadingElement>(null);
  const urlRef = useRef<HTMLInputElement>(null);
  const codeRef = useRef<HTMLInputElement>(null);
  // Sitio al que llevar el foco al cerrar ("Ir al sitio"), en lugar del botón que abrió el diálogo.
  const focusSiteOnClose = useRef<string | null>(null);
  const urlId = useId();
  const urlHelpId = useId();
  const urlErrorId = useId();
  const codeId = useId();
  const codeHelpId = useId();
  const codeErrorId = useId();
  const formId = useId();

  const [step, setStep] = useState<Step>(reconnecting ? 2 : 1);
  const [url, setUrl] = useState(reconnecting ? mode.site.url : "");
  // El código solo vive aquí. Se vacía al terminar con éxito y se descarta al cerrar.
  const [code, setCode] = useState("");
  const [fieldError, setFieldError] = useState<FieldError | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [exportState, setExportState] = useState<ExportState>({ status: "idle" });
  const shownStep = useRef(step);

  // Al cambiar de paso, el foco va al título del paso (lo anuncia el lector de pantalla).
  useEffect(() => {
    if (shownStep.current !== step) {
      shownStep.current = step;
      stepHeadingRef.current?.focus();
    }
  }, [step]);

  // Tras un error, el foco va al campo que hay que corregir.
  useEffect(() => {
    if (fieldError?.field === "url") {
      urlRef.current?.focus();
    } else if (fieldError?.field === "code") {
      codeRef.current?.focus();
    }
  }, [fieldError]);

  function close() {
    setCode("");
    onClose();
  }

  function goTo(next: Step) {
    setFieldError(null);
    setStep(next);
  }

  async function handleExport() {
    setExportState({ status: "saving" });
    try {
      const result = await exportWpPlugin();
      setExportState({ status: "done", fileName: result.file_name });
    } catch (reason: unknown) {
      setExportState({ status: "error", error: toFaroError(reason) });
    }
  }

  function showError(error: FaroError) {
    const message = getErrorMessage(error, i18n);
    if (error.code === "site.already_connected") {
      const siteId = error.details.site_id;
      setFieldError({
        field: "form",
        message,
        ...(typeof siteId === "string" ? { existingSiteId: siteId } : {}),
      });
    } else if (URL_ERRORS.has(error.code)) {
      setFieldError({ field: "url", message });
    } else if (CODE_ERRORS.has(error.code)) {
      setFieldError({ field: "code", message });
    } else {
      setFieldError({ field: "form", message });
    }
  }

  async function handleSubmit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting) {
      return;
    }
    const address = url.trim();
    if (!reconnecting && address === "") {
      setFieldError({ field: "url", message: t("sites.connect.step3.urlRequired") });
      return;
    }
    const pairingCode = normalizePairingCode(code);
    if (pairingCode === null) {
      showError(new FaroError("site.invalid_code_format"));
      return;
    }
    setFieldError(null);
    setSubmitting(true);
    try {
      const site = reconnecting
        ? await reconnectSite(mode.site.id, pairingCode)
        : await connectSite(address, pairingCode);
      setCode("");
      setUrl("");
      markSiteCheckedThisSession(site.id);
      markSiteVerified(site.id);
      upsertSite(queryClient, site);
      void refreshSites(queryClient);
      toast.success(t("sites.toast.connected", { name: siteDisplayName(site) }));
      onClose();
    } catch (reason: unknown) {
      const error = toFaroError(reason);
      if (error.code === "site.not_found") {
        void refreshSites(queryClient);
      }
      showError(error);
      setSubmitting(false);
    }
  }

  function preventCloseWhileSubmitting(event: Event) {
    if (submitting) {
      event.preventDefault();
    }
  }

  const title = reconnecting
    ? t("sites.connect.reconnectTitle", { name: siteDisplayName(mode.site) })
    : t("sites.connect.title");

  const urlError = fieldError?.field === "url" ? fieldError.message : null;
  const codeError = fieldError?.field === "code" ? fieldError.message : null;
  const formError = fieldError?.field === "form" ? fieldError : null;

  const backButton = (
    <Button
      variant="secondary"
      disabled={submitting}
      onClick={() => {
        goTo((step - 1) as Step);
      }}
    >
      {t("sites.connect.back")}
    </Button>
  );

  return (
    <DialogContent
      aria-busy={submitting}
      onEscapeKeyDown={preventCloseWhileSubmitting}
      onPointerDownOutside={preventCloseWhileSubmitting}
      onInteractOutside={preventCloseWhileSubmitting}
      onCloseAutoFocus={(event) => {
        const siteId = focusSiteOnClose.current;
        if (siteId !== null) {
          event.preventDefault();
          onGoToSite(siteId);
        }
      }}
    >
      <DialogHeader>
        <DialogTitle>{title}</DialogTitle>
        <DialogDescription>
          {t("sites.connect.stepIndicator", { step, total: TOTAL_STEPS })}
        </DialogDescription>
      </DialogHeader>

      <h3 ref={stepHeadingRef} tabIndex={-1} className="text-base font-semibold outline-none">
        {t(`sites.connect.step${step}.title`)}
      </h3>

      {step === 1 ? (
        <div className="flex flex-col gap-4">
          <div className="flex flex-col items-start gap-2">
            <Button
              variant="secondary"
              disabled={exportState.status === "saving"}
              onClick={() => {
                void handleExport();
              }}
            >
              {exportState.status === "saving" ? (
                <LoaderCircle
                  aria-hidden="true"
                  className="size-4 animate-spin motion-reduce:animate-none"
                  strokeWidth={1.5}
                />
              ) : (
                <Download aria-hidden="true" className="size-4" strokeWidth={1.5} />
              )}
              {exportState.status === "saving"
                ? t("sites.connect.step1.downloading")
                : t("sites.connect.step1.download")}
            </Button>
            <div role="status" aria-live="polite" aria-atomic="true">
              {exportState.status === "done" ? (
                <p className="flex items-start gap-2 text-sm">
                  <CircleCheck
                    aria-hidden="true"
                    className="mt-0.5 size-4 shrink-0 text-success"
                    strokeWidth={1.5}
                  />
                  {t("sites.connect.step1.downloaded", { fileName: exportState.fileName })}
                </p>
              ) : null}
            </div>
            {exportState.status === "error" ? (
              <p role="alert" className="flex items-start gap-2 text-sm">
                <CircleX
                  aria-hidden="true"
                  className="mt-0.5 size-4 shrink-0 text-critical"
                  strokeWidth={1.5}
                />
                {getErrorMessage(exportState.error, i18n)}
              </p>
            ) : null}
          </div>
          <div className="flex flex-col gap-2">
            <p className="text-sm font-medium">{t("sites.connect.step1.instructionsLabel")}</p>
            <ol className="flex list-decimal flex-col gap-1 pl-6 text-base">
              <li>{t("sites.connect.step1.instruction1")}</li>
              <li>{t("sites.connect.step1.instruction2")}</li>
              <li>{t("sites.connect.step1.instruction3")}</li>
            </ol>
          </div>
          <Button
            variant="ghost"
            className="self-start text-primary underline-offset-4 hover:underline"
            onClick={() => {
              goTo(2);
            }}
          >
            {t("sites.connect.step1.alreadyInstalled")}
          </Button>
        </div>
      ) : null}

      {step === 2 ? <p className="text-base">{t("sites.connect.step2.description")}</p> : null}

      {step === 3 ? (
        <form
          id={formId}
          noValidate
          className="flex flex-col gap-4"
          onSubmit={(event) => {
            void handleSubmit(event);
          }}
        >
          {formError ? (
            <div role="alert" className="flex flex-col items-start gap-2 text-sm">
              <p className="flex items-start gap-2">
                <CircleX
                  aria-hidden="true"
                  className="mt-0.5 size-4 shrink-0 text-critical"
                  strokeWidth={1.5}
                />
                {formError.message}
              </p>
              {formError.existingSiteId !== undefined ? (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => {
                    focusSiteOnClose.current = formError.existingSiteId ?? null;
                    close();
                  }}
                >
                  {t("sites.connect.step3.goToSite")}
                </Button>
              ) : null}
            </div>
          ) : null}

          <div className="flex flex-col gap-2">
            <Label htmlFor={urlId}>{t("sites.connect.step3.urlLabel")}</Label>
            <Input
              ref={urlRef}
              id={urlId}
              type="url"
              autoComplete="off"
              autoCapitalize="off"
              spellCheck={false}
              placeholder={t("sites.connect.step3.urlPlaceholder")}
              className="font-mono"
              value={url}
              readOnly={reconnecting}
              disabled={submitting}
              onChange={(event) => {
                setUrl(event.target.value);
              }}
              aria-invalid={urlError !== null}
              aria-describedby={urlError !== null ? `${urlHelpId} ${urlErrorId}` : urlHelpId}
            />
            <p id={urlHelpId} className="text-sm text-muted-foreground">
              {t("sites.connect.step3.urlHelp")}
            </p>
            {urlError !== null ? (
              <p id={urlErrorId} role="alert" className="flex items-start gap-2 text-sm">
                <CircleX
                  aria-hidden="true"
                  className="mt-0.5 size-4 shrink-0 text-critical"
                  strokeWidth={1.5}
                />
                {urlError}
              </p>
            ) : null}
          </div>

          <div className="flex flex-col gap-2">
            <Label htmlFor={codeId}>{t("sites.connect.step3.codeLabel")}</Label>
            <Input
              ref={codeRef}
              id={codeId}
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              spellCheck={false}
              maxLength={12}
              className="font-mono tracking-widest"
              value={code}
              disabled={submitting}
              onChange={(event) => {
                setCode(event.target.value);
              }}
              aria-invalid={codeError !== null}
              aria-describedby={codeError !== null ? `${codeHelpId} ${codeErrorId}` : codeHelpId}
            />
            <p id={codeHelpId} className="text-sm text-muted-foreground">
              {t("sites.connect.step3.codeHelp")}
            </p>
            {codeError !== null ? (
              <p id={codeErrorId} role="alert" className="flex items-start gap-2 text-sm">
                <CircleX
                  aria-hidden="true"
                  className="mt-0.5 size-4 shrink-0 text-critical"
                  strokeWidth={1.5}
                />
                {codeError}
              </p>
            ) : null}
          </div>

          <p role="status" aria-live="polite" className="text-sm text-muted-foreground">
            {submitting ? t("sites.connect.step3.slowNote") : null}
          </p>
        </form>
      ) : null}

      <DialogFooter>
        <Button variant="secondary" onClick={close} disabled={submitting}>
          {t("sites.connect.cancel")}
        </Button>
        {step > 1 ? backButton : null}
        {step < 3 ? (
          <Button
            onClick={() => {
              goTo((step + 1) as Step);
            }}
          >
            {t("sites.connect.next")}
          </Button>
        ) : (
          <Button type="submit" form={formId} disabled={submitting}>
            {submitting ? (
              <>
                <LoaderCircle
                  aria-hidden="true"
                  className="size-4 animate-spin motion-reduce:animate-none"
                  strokeWidth={1.5}
                />
                {t("sites.connect.step3.submitting")}
              </>
            ) : reconnecting ? (
              t("sites.connect.step3.reconnectSubmit")
            ) : (
              t("sites.connect.step3.submit")
            )}
          </Button>
        )}
      </DialogFooter>

      <p className="flex items-center gap-2 border-t border-border pt-4 text-sm text-muted-foreground">
        <Lock aria-hidden="true" className="size-4 shrink-0" strokeWidth={1.5} />
        {t("sites.connect.passwordNote")}
      </p>
    </DialogContent>
  );
}
