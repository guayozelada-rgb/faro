/**
 * Archivo generado por `npm run contracts` (scripts/generate-contracts.mjs) con openapi-typescript.
 * No lo edites a mano: cambia el motor (apps/engine) y vuelve a generarlo.
 */

export interface paths {
    "/health": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Estado del motor */
        get: operations["getHealth"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/llm/limits/{provider}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** Cambiar el tope diario de gasto de una clave de IA */
        put: operations["setLlmDailyLimit"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/llm/preferences": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** Elegir la clave de IA que usan los agentes */
        put: operations["setLlmPreferences"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/llm/usage": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Gasto de hoy y tope de cada clave de IA */
        get: operations["getLlmUsage"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/sites": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Sitios conectados */
        get: operations["listSites"];
        put?: never;
        /** Conectar un sitio con un código de WordPress */
        post: operations["connectSite"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/sites/{site_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        /** Desconectar y quitar un sitio */
        delete: operations["removeSite"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/sites/{site_id}/check": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Comprobar la conexión (una revocación es un resultado, no un error) */
        post: operations["checkSiteConnection"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/sites/{site_id}/connection": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        /** Volver a conectar un sitio con un código nuevo */
        put: operations["reconnectSite"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/sites/{site_id}/content": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Páginas, entradas o productos publicados (leídos en vivo) */
        get: operations["listSiteContent"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        /**
         * ConnectSiteIn
         * @description Cuerpo de `connectSite`. El código solo vive en esta petición: no se guarda ni se
         *     registra.
         */
        ConnectSiteIn: {
            /**
             * Pairing Code
             * @description 6 números; admite espacios.
             */
            pairing_code: string;
            /**
             * Url
             * @example https://tutienda.com
             */
            url: string;
        };
        /**
         * DatabaseHealth
         * @description Estado de la base del perfil (ADR 0009 §4). El motor sigue `ok` aunque no esté lista.
         */
        DatabaseHealth: {
            /**
             * Error Code
             * @description Código `db.*` (o `vault.keyring_unavailable`) si no está disponible.
             * @example db.key_missing
             */
            error_code: string | null;
            /**
             * Newer Schema
             * @description La base tiene migraciones de una versión más nueva de Faro (aviso).
             */
            newer_schema: boolean;
            /**
             * State
             * @enum {string}
             */
            state: "ready" | "unavailable";
        };
        /**
         * ErrorOut
         * @description Formato de error común a todas las capas (ADR 0002).
         */
        ErrorOut: {
            /**
             * Code
             * @description Código estable `dominio.motivo`.
             * @example engine.unauthorized
             */
            code: string;
            /** Details */
            details?: {
                [key: string]: unknown;
            };
            /**
             * Message
             * @description Mensaje en español para el usuario.
             */
            message: string;
        };
        /** HTTPValidationError */
        HTTPValidationError: {
            /** Detail */
            detail?: components["schemas"]["ValidationError"][];
        };
        /**
         * HealthOut
         * @description Estado del motor.
         */
        HealthOut: {
            database: components["schemas"]["DatabaseHealth"];
            /**
             * Status
             * @default ok
             * @constant
             */
            status: "ok";
            /**
             * Version
             * @example 0.1.0
             */
            version: string;
        };
        /** LlmDailyLimitIn */
        LlmDailyLimitIn: {
            /**
             * Daily Limit Micros
             * @description Tope diario en micros de USD, de 500 000 (US$0,50) a 500 000 000 (US$500).
             * @example 5000000
             */
            daily_limit_micros: number;
        };
        /** LlmPreferencesIn */
        LlmPreferencesIn: {
            /**
             * Preferred Provider
             * @description Proveedor de la clave que usan los agentes; `null` = automático.
             */
            preferred_provider: ("anthropic" | "openai" | "gemini") | null;
        };
        /**
         * LlmProviderUsageOut
         * @description Gasto de hoy y tope de la clave de un proveedor. Nunca la clave ni su `last4`.
         */
        LlmProviderUsageOut: {
            /** Daily Limit Micros */
            daily_limit_micros: number;
            /**
             * Has Key
             * @description Hay clave en la Bóveda (según el último aviso del núcleo).
             */
            has_key: boolean;
            /** Limit Reached */
            limit_reached: boolean;
            /**
             * Provider
             * @enum {string}
             */
            provider: "anthropic" | "openai" | "gemini";
            /** Requests Today */
            requests_today: number;
            /** Spent Today Micros */
            spent_today_micros: number;
            /** Tokens Today */
            tokens_today: number;
        };
        /** LlmUsageOut */
        LlmUsageOut: {
            /**
             * Currency
             * @default USD
             * @constant
             */
            currency: "USD";
            /** Preferred Provider */
            preferred_provider: ("anthropic" | "openai" | "gemini") | null;
            /** Providers */
            providers: components["schemas"]["LlmProviderUsageOut"][];
            /** Total Today Micros */
            total_today_micros: number;
            /**
             * Usage Date
             * @description Día local AAAA-MM-DD.
             * @example 2026-10-09
             */
            usage_date: string;
        };
        /**
         * ReconnectSiteIn
         * @description Cuerpo de `reconnectSite`.
         */
        ReconnectSiteIn: {
            /**
             * Pairing Code
             * @description 6 números; admite espacios.
             */
            pairing_code: string;
        };
        /** RemoveSiteOut */
        RemoveSiteOut: {
            /**
             * Remote Revoked
             * @description `true` si el sitio confirmó la desconexión (o ya estaba desconectado).
             */
            remote_revoked: boolean;
        };
        /** SiteConnectionOut */
        SiteConnectionOut: {
            /**
             * Connected At
             * @example 2026-09-30T12:00:00Z
             */
            connected_at: string;
            counts: components["schemas"]["SiteCountsOut"] | null;
            /** Last Checked At */
            last_checked_at: string | null;
            /**
             * Last Error Code
             * @description Por qué está desconectado: `site.revoked`, `site.connection_broken`, `site.auth_failed` o `site.secret_missing`.
             * @example site.revoked
             */
            last_error_code: string | null;
            /** Plugin Version */
            plugin_version: string | null;
            /** Revoked At */
            revoked_at: string | null;
            /** Seo Plugin */
            seo_plugin: ("yoast" | "rank_math" | "none") | null;
            /**
             * Status
             * @enum {string}
             */
            status: "active" | "revoked";
            woocommerce: components["schemas"]["WooCommerceOut"] | null;
            /** Wp Version */
            wp_version: string | null;
        };
        /** SiteContentItem */
        SiteContentItem: {
            /**
             * Kind
             * @enum {string}
             */
            kind: "page" | "post" | "product";
            /** Modified At */
            modified_at: string;
            /** Remote Id */
            remote_id: number;
            /** Slug */
            slug: string;
            /**
             * Title
             * @description Texto plano (nunca HTML).
             */
            title: string;
            /** Url */
            url: string;
        };
        /** SiteContentPage */
        SiteContentPage: {
            /** Items */
            items: components["schemas"]["SiteContentItem"][];
            /**
             * Next Cursor
             * @description Número de la página siguiente, como texto.
             */
            next_cursor: string | null;
            /** Total */
            total: number;
            /** Total Pages */
            total_pages: number;
            /** Woocommerce Active */
            woocommerce_active: boolean;
        };
        /** SiteCountsOut */
        SiteCountsOut: {
            /** Pages */
            pages: number;
            /** Posts */
            posts: number;
            /**
             * Products
             * @description `null` si WooCommerce no está activo.
             */
            products: number | null;
        };
        /** SiteListOut */
        SiteListOut: {
            /** Items */
            items: components["schemas"]["SiteOut"][];
            /**
             * Next Cursor
             * @description Siempre `null` en F1a.
             */
            next_cursor?: string | null;
        };
        /**
         * SiteOut
         * @description Un sitio conectado. `connection` es `null` solo en sitios sin plugin (reservado F2).
         */
        SiteOut: {
            connection: components["schemas"]["SiteConnectionOut"] | null;
            /** Created At */
            created_at: string;
            /**
             * Id
             * @example 01920000-0000-7000-8000-000000000001
             */
            id: string;
            /** Name */
            name: string | null;
            /**
             * Url
             * @example https://tutienda.com
             */
            url: string;
        };
        /** ValidationError */
        ValidationError: {
            /** Context */
            ctx?: Record<string, never>;
            /** Input */
            input?: unknown;
            /** Location */
            loc: (string | number)[];
            /** Message */
            msg: string;
            /** Error Type */
            type: string;
        };
        /** WooCommerceOut */
        WooCommerceOut: {
            /** Active */
            active: boolean;
            /** Hpos Enabled */
            hpos_enabled: boolean | null;
            /** Version */
            version: string | null;
        };
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    getHealth: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HealthOut"];
                };
            };
            /** @description Token ausente o inválido. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Cabecera Host no permitida. */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    setLlmDailyLimit: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description Proveedor de IA. */
                provider: "anthropic" | "openai" | "gemini";
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["LlmDailyLimitIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["LlmUsageOut"];
                };
            };
            /** @description Token ausente o inválido. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Cabecera Host no permitida. */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description `llm.no_key`: no hay clave de ese proveedor. */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description `llm.invalid_provider` o `llm.invalid_limit`. */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Base de datos no disponible. */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    setLlmPreferences: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["LlmPreferencesIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["LlmUsageOut"];
                };
            };
            /** @description Token ausente o inválido. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Cabecera Host no permitida. */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description `llm.no_key`: no hay clave de ese proveedor. */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description `llm.invalid_provider` o `llm.invalid_limit`. */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Base de datos no disponible. */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    getLlmUsage: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["LlmUsageOut"];
                };
            };
            /** @description Token ausente o inválido. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Cabecera Host no permitida. */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Base de datos no disponible. */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    listSites: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SiteListOut"];
                };
            };
            /** @description Token ausente o inválido. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Cabecera Host no permitida. */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Base de datos o llavero no disponibles. */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    connectSite: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ConnectSiteIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SiteOut"];
                };
            };
            /** @description Dirección o código no válidos. */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Token ausente o inválido. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Cabecera Host no permitida. */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description `site.not_found`. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Conexión revocada o sitio ya conectado. */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
            /** @description `site.rate_limited`. */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description El sitio respondió mal o no se pudo usar. */
            502: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Base de datos o llavero no disponibles. */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description `site.timeout`. */
            504: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    removeSite: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description UUID del sitio en minúsculas. */
                site_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RemoveSiteOut"];
                };
            };
            /** @description Dirección o código no válidos. */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Token ausente o inválido. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Cabecera Host no permitida. */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description `site.not_found`. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Conexión revocada o sitio ya conectado. */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
            /** @description `site.rate_limited`. */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description El sitio respondió mal o no se pudo usar. */
            502: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Base de datos o llavero no disponibles. */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description `site.timeout`. */
            504: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    checkSiteConnection: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description UUID del sitio en minúsculas. */
                site_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SiteOut"];
                };
            };
            /** @description Dirección o código no válidos. */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Token ausente o inválido. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Cabecera Host no permitida. */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description `site.not_found`. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Conexión revocada o sitio ya conectado. */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
            /** @description `site.rate_limited`. */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description El sitio respondió mal o no se pudo usar. */
            502: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Base de datos o llavero no disponibles. */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description `site.timeout`. */
            504: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    reconnectSite: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description UUID del sitio en minúsculas. */
                site_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ReconnectSiteIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SiteOut"];
                };
            };
            /** @description Dirección o código no válidos. */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Token ausente o inválido. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Cabecera Host no permitida. */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description `site.not_found`. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Conexión revocada o sitio ya conectado. */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
            /** @description `site.rate_limited`. */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description El sitio respondió mal o no se pudo usar. */
            502: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Base de datos o llavero no disponibles. */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description `site.timeout`. */
            504: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
    listSiteContent: {
        parameters: {
            query: {
                kind: "page" | "post" | "product";
                /** @description Número de página como texto. */
                cursor?: string | null;
                limit?: number;
            };
            header?: never;
            path: {
                /** @description UUID del sitio en minúsculas. */
                site_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SiteContentPage"];
                };
            };
            /** @description Dirección o código no válidos. */
            400: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Token ausente o inválido. */
            401: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Cabecera Host no permitida. */
            403: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description `site.not_found`. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Conexión revocada o sitio ya conectado. */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
            /** @description `site.rate_limited`. */
            429: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description El sitio respondió mal o no se pudo usar. */
            502: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description Base de datos o llavero no disponibles. */
            503: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description `site.timeout`. */
            504: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
        };
    };
}
