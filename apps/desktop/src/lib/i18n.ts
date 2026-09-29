import i18n from "i18next";
import { initReactI18next } from "react-i18next";

import enAds from "@/locales/en/ads.json";
import enAgents from "@/locales/en/agents.json";
import enAudit from "@/locales/en/audit.json";
import enCommon from "@/locales/en/common.json";
import enContent from "@/locales/en/content.json";
import enErrors from "@/locales/en/errors.json";
import enHome from "@/locales/en/home.json";
import enInbox from "@/locales/en/inbox.json";
import enResearch from "@/locales/en/research.json";
import enSettings from "@/locales/en/settings.json";
import esAds from "@/locales/es/ads.json";
import esAgents from "@/locales/es/agents.json";
import esAudit from "@/locales/es/audit.json";
import esCommon from "@/locales/es/common.json";
import esContent from "@/locales/es/content.json";
import esErrors from "@/locales/es/errors.json";
import esHome from "@/locales/es/home.json";
import esInbox from "@/locales/es/inbox.json";
import esResearch from "@/locales/es/research.json";
import esSettings from "@/locales/es/settings.json";
import ptAds from "@/locales/pt-BR/ads.json";
import ptAgents from "@/locales/pt-BR/agents.json";
import ptAudit from "@/locales/pt-BR/audit.json";
import ptCommon from "@/locales/pt-BR/common.json";
import ptContent from "@/locales/pt-BR/content.json";
import ptErrors from "@/locales/pt-BR/errors.json";
import ptHome from "@/locales/pt-BR/home.json";
import ptInbox from "@/locales/pt-BR/inbox.json";
import ptResearch from "@/locales/pt-BR/research.json";
import ptSettings from "@/locales/pt-BR/settings.json";

export const NAMESPACES = [
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
] as const;

export type Namespace = (typeof NAMESPACES)[number];

export const LANGUAGES = ["es", "en", "pt-BR"] as const;

export type Language = (typeof LANGUAGES)[number];

/** Recursos importados estáticamente (sin backend HTTP). */
export const resources = {
  es: {
    common: esCommon,
    home: esHome,
    inbox: esInbox,
    research: esResearch,
    content: esContent,
    audit: esAudit,
    ads: esAds,
    agents: esAgents,
    settings: esSettings,
    errors: esErrors,
  },
  en: {
    common: enCommon,
    home: enHome,
    inbox: enInbox,
    research: enResearch,
    content: enContent,
    audit: enAudit,
    ads: enAds,
    agents: enAgents,
    settings: enSettings,
    errors: enErrors,
  },
  "pt-BR": {
    common: ptCommon,
    home: ptHome,
    inbox: ptInbox,
    research: ptResearch,
    content: ptContent,
    audit: ptAudit,
    ads: ptAds,
    agents: ptAgents,
    settings: ptSettings,
    errors: ptErrors,
  },
} satisfies Record<Language, Record<Namespace, unknown>>;

// En F0 el idioma es `es` fijo; `en` y `pt-BR` quedan preparados.
void i18n.use(initReactI18next).init({
  lng: "es",
  fallbackLng: "es",
  supportedLngs: [...LANGUAGES],
  ns: [...NAMESPACES],
  defaultNS: "common",
  resources,
  initAsync: false,
  interpolation: { escapeValue: false },
  returnNull: false,
  showSupportNotice: false,
});

export default i18n;
