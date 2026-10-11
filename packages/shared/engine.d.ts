/**
 * Archivo generado por `npm run contracts` (scripts/generate-contracts.mjs) con openapi-typescript.
 * No lo edites a mano: cambia el motor (apps/engine) y vuelve a generarlo.
 */

export interface paths {
    "/agent-runs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Tareas de los agentes, la más reciente primero */
        get: operations["listAgentRuns"];
        put?: never;
        /** Lanzar un agente (la tarea queda en cola) */
        post: operations["startAgentRun"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/agent-runs/estimate": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Costo estimado y máximo de una tarea antes de lanzarla */
        post: operations["estimateAgentRun"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/agent-runs/notices/ack": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Entendido: el usuario vio el aviso de tareas recuperadas */
        post: operations["acknowledgeAgentNotices"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/agent-runs/{run_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Detalle de una tarea: pasos, costos, resultado y propuesta */
        get: operations["getAgentRun"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/agent-runs/{run_id}/cancel": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        /** Cancelar una tarea */
        post: operations["cancelAgentRun"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/agents": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Agentes disponibles */
        get: operations["listAgents"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
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
    "/schedules": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        /** Programaciones de los agentes */
        get: operations["listSchedules"];
        put?: never;
        /** Programar un agente en un sitio (cada día o cada semana) */
        post: operations["createSchedule"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/schedules/{schedule_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        /** Quitar una programación */
        delete: operations["deleteSchedule"];
        options?: never;
        head?: never;
        /** Activar, desactivar o cambiar la frecuencia o la hora */
        patch: operations["updateSchedule"];
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
        /** AcknowledgeNoticesIn */
        AcknowledgeNoticesIn: {
            /** Run Ids */
            run_ids: string[];
        };
        /** AcknowledgeNoticesOut */
        AcknowledgeNoticesOut: {
            /** Acknowledged */
            acknowledged: number;
        };
        /** AgentActionOut */
        AgentActionOut: {
            /** Action Kind */
            action_kind: string;
            /**
             * Side Effect
             * @enum {string}
             */
            side_effect: "internal" | "publish" | "spend";
        };
        /** AgentListOut */
        AgentListOut: {
            /** Items */
            items: components["schemas"]["AgentOut"][];
        };
        /** AgentOut */
        AgentOut: {
            /** Actions */
            actions: components["schemas"]["AgentActionOut"][];
            /** Kind */
            kind: string;
            /** Requires Site */
            requires_site: boolean;
            /** Version */
            version: number;
        };
        /** AgentRunDetailOut */
        AgentRunDetailOut: {
            /** Activity Seq */
            activity_seq: number;
            /** Agent Kind */
            agent_kind: string;
            approval: components["schemas"]["ApprovalOut"] | null;
            /** Cost Micros */
            cost_micros: number;
            /** Created At */
            created_at: string;
            /**
             * Currency
             * @default USD
             * @constant
             */
            currency: "USD";
            /** Current Step */
            current_step: string | null;
            /** Error Code */
            error_code: string | null;
            /** Estimated Cost Micros */
            estimated_cost_micros: number | null;
            /** Finished At */
            finished_at: string | null;
            /** Id */
            id: string;
            /** Max Cost Micros */
            max_cost_micros: number | null;
            /**
             * Notice Pending
             * @description Recuperada al abrir Faro y aún sin **Entendido** del usuario.
             */
            notice_pending: boolean;
            /** Parent Run Id */
            parent_run_id: string | null;
            /** Pending Approval Id */
            pending_approval_id: string | null;
            /** Provider */
            provider: ("anthropic" | "openai" | "gemini") | null;
            /**
             * Result
             * @description Resultado del agente (JSON validado; T9 lo tipa para `site_summary`).
             */
            result: {
                [key: string]: unknown;
            } | null;
            /** Site Id */
            site_id: string | null;
            /** Started At */
            started_at: string | null;
            /**
             * Status
             * @enum {string}
             */
            status: "queued" | "running" | "waiting_approval" | "paused" | "succeeded" | "failed" | "cancelled";
            /**
             * Status Reason
             * @description `daily_limit`, `agents_paused`, `interrupted`, `cancel_requested`…
             */
            status_reason: string | null;
            /** Steps */
            steps: components["schemas"]["AgentStepOut"][];
            /** Token Budget */
            token_budget: number;
            /** Tokens */
            tokens: number;
            /**
             * Trigger
             * @enum {string}
             */
            trigger: "user" | "schedule" | "catch_up";
        };
        /** AgentRunOut */
        AgentRunOut: {
            /** Activity Seq */
            activity_seq: number;
            /** Agent Kind */
            agent_kind: string;
            /** Cost Micros */
            cost_micros: number;
            /** Created At */
            created_at: string;
            /**
             * Currency
             * @default USD
             * @constant
             */
            currency: "USD";
            /** Current Step */
            current_step: string | null;
            /** Error Code */
            error_code: string | null;
            /** Estimated Cost Micros */
            estimated_cost_micros: number | null;
            /** Finished At */
            finished_at: string | null;
            /** Id */
            id: string;
            /** Max Cost Micros */
            max_cost_micros: number | null;
            /**
             * Notice Pending
             * @description Recuperada al abrir Faro y aún sin **Entendido** del usuario.
             */
            notice_pending: boolean;
            /** Parent Run Id */
            parent_run_id: string | null;
            /** Pending Approval Id */
            pending_approval_id: string | null;
            /** Provider */
            provider: ("anthropic" | "openai" | "gemini") | null;
            /** Site Id */
            site_id: string | null;
            /** Started At */
            started_at: string | null;
            /**
             * Status
             * @enum {string}
             */
            status: "queued" | "running" | "waiting_approval" | "paused" | "succeeded" | "failed" | "cancelled";
            /**
             * Status Reason
             * @description `daily_limit`, `agents_paused`, `interrupted`, `cancel_requested`…
             */
            status_reason: string | null;
            /** Token Budget */
            token_budget: number;
            /** Tokens */
            tokens: number;
            /**
             * Trigger
             * @enum {string}
             */
            trigger: "user" | "schedule" | "catch_up";
        };
        /** AgentRunPage */
        AgentRunPage: {
            /** Items */
            items: components["schemas"]["AgentRunOut"][];
            /** Next Cursor */
            next_cursor: string | null;
        };
        /** AgentStepOut */
        AgentStepOut: {
            /** Cost Estimated */
            cost_estimated: boolean;
            /** Cost Micros */
            cost_micros: number;
            /** Error Code */
            error_code: string | null;
            /** Finished At */
            finished_at: string | null;
            /** Id */
            id: string;
            /**
             * Kind
             * @enum {string}
             */
            kind: "llm_call" | "tool_call" | "approval" | "control";
            /** Model */
            model: string | null;
            /** Node */
            node: string;
            /** Seq */
            seq: number;
            /** Started At */
            started_at: string;
            /**
             * Status
             * @enum {string}
             */
            status: "running" | "succeeded" | "failed" | "skipped" | "cancelled";
            /** Tier */
            tier: ("economy" | "premium") | null;
            /** Tokens In */
            tokens_in: number;
            /** Tokens Out */
            tokens_out: number;
        };
        /** ApprovalOut */
        ApprovalOut: {
            /** Action Kind */
            action_kind: string;
            /** Agent Kind */
            agent_kind: string;
            /** Autonomy Level */
            autonomy_level: number;
            /** Created At */
            created_at: string;
            /**
             * Currency
             * @default USD
             * @constant
             */
            currency: "USD";
            /** Decided At */
            decided_at: string | null;
            /** Error Code */
            error_code: string | null;
            /** Estimated Cost Micros */
            estimated_cost_micros: number | null;
            /** Evidence */
            evidence: {
                [key: string]: unknown;
            };
            /** Executed At */
            executed_at: string | null;
            /** Expires At */
            expires_at: string;
            /** Id */
            id: string;
            /**
             * Payload
             * @description JSON validado de la acción (T9 lo tipa).
             */
            payload: {
                [key: string]: unknown;
            };
            /** Run Id */
            run_id: string;
            /**
             * Side Effect
             * @enum {string}
             */
            side_effect: "internal" | "publish" | "spend";
            /** Site Id */
            site_id: string | null;
            /**
             * Status
             * @enum {string}
             */
            status: "pending" | "approved" | "rejected" | "expired" | "cancelled" | "executed" | "failed";
        };
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
        /** CostEstimateOut */
        CostEstimateOut: {
            /** Agent Kind */
            agent_kind: string;
            /**
             * Blocking Code
             * @description Por qué no se puede lanzar ahora (`null` = se puede).
             */
            blocking_code: ("agents.paused" | "llm.no_key" | "llm.daily_limit_reached" | "agent.site_not_active") | null;
            /**
             * Currency
             * @default USD
             * @constant
             */
            currency: "USD";
            /** Daily Limit Micros */
            daily_limit_micros: number;
            /** Expected Cost Micros */
            expected_cost_micros: number;
            /** Fits Daily Limit */
            fits_daily_limit: boolean;
            /**
             * Max Cost Micros
             * @description Máximo garantizado de la tarea.
             */
            max_cost_micros: number;
            /** Models */
            models: components["schemas"]["ModelOut"][];
            /** Provider */
            provider: ("anthropic" | "openai" | "gemini") | null;
            /** Site Id */
            site_id: string | null;
            /** Spent Today Micros */
            spent_today_micros: number;
            /** Token Budget */
            token_budget: number;
        };
        /** CreateScheduleIn */
        CreateScheduleIn: {
            /**
             * Agent Kind
             * @description Tipo de agente.
             * @example site_summary
             */
            agent_kind: string;
            /**
             * Cadence
             * @enum {string}
             */
            cadence: "daily" | "weekly";
            /**
             * Site Id
             * @description UUID del sitio.
             */
            site_id: string;
            /**
             * Time Local
             * @example 09:00
             */
            time_local: string;
            /** Weekday */
            weekday?: number | null;
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
        /** EstimateAgentRunIn */
        EstimateAgentRunIn: {
            /**
             * Agent Kind
             * @description Tipo de agente.
             * @example site_summary
             */
            agent_kind: string;
            /** Site Id */
            site_id?: string | null;
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
        /** ModelOut */
        ModelOut: {
            /** Model */
            model: string;
            /**
             * Tier
             * @enum {string}
             */
            tier: "economy" | "premium";
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
        /** ScheduleListOut */
        ScheduleListOut: {
            /** Items */
            items: components["schemas"]["ScheduleOut"][];
            /** Next Cursor */
            next_cursor?: null;
        };
        /** ScheduleOut */
        ScheduleOut: {
            /** Agent Kind */
            agent_kind: string;
            /**
             * Cadence
             * @enum {string}
             */
            cadence: "daily" | "weekly";
            /** Enabled */
            enabled: boolean;
            /** Id */
            id: string;
            /** Last Run At */
            last_run_at: string | null;
            /** Last Run Id */
            last_run_id: string | null;
            /** Next Run At */
            next_run_at: string;
            /** Site Id */
            site_id: string;
            /**
             * Time Local
             * @example 09:00
             */
            time_local: string;
            /**
             * Timezone
             * @description Zona IANA del sistema al guardarla.
             */
            timezone: string;
            /**
             * Weekday
             * @description 0 = lunes … 6 = domingo; solo semanales.
             */
            weekday: number | null;
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
        /** StartAgentRunIn */
        StartAgentRunIn: {
            /**
             * Accepted Max Cost Micros
             * @description El máximo que vio y aceptó el usuario (de `estimateAgentRun`).
             */
            accepted_max_cost_micros: number;
            /**
             * Agent Kind
             * @description Tipo de agente.
             * @example site_summary
             */
            agent_kind: string;
            /** Site Id */
            site_id?: string | null;
        };
        /** UpdateScheduleIn */
        UpdateScheduleIn: {
            /** Cadence */
            cadence?: ("daily" | "weekly") | null;
            /** Enabled */
            enabled?: boolean | null;
            /** Time Local */
            time_local?: string | null;
            /** Weekday */
            weekday?: number | null;
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
    listAgentRuns: {
        parameters: {
            query?: {
                status?: ("queued" | "running" | "waiting_approval" | "paused" | "succeeded" | "failed" | "cancelled") | null;
                site_id?: string | null;
                notice_pending?: boolean | null;
                cursor?: string | null;
                limit?: number;
            };
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
                    "application/json": components["schemas"]["AgentRunPage"];
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
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
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
    startAgentRun: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["StartAgentRunIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentRunOut"];
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
            /** @description `agent.unknown` o `site.not_found`. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description `agents.paused`, `agent.site_not_active`, `llm.no_key`, `agent.estimate_changed` (`details.max_cost_micros`), `llm.daily_limit_reached` o `agent.already_queued`. */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description `agent.site_required`. */
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
    estimateAgentRun: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["EstimateAgentRunIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CostEstimateOut"];
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
            /** @description `agent.unknown` o `site.not_found`. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description `agent.site_required`. */
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
    acknowledgeAgentNotices: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AcknowledgeNoticesIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AcknowledgeNoticesOut"];
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
            /** @description Validation Error */
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
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
    getAgentRun: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description UUID de la tarea en minúsculas. */
                run_id: string;
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
                    "application/json": components["schemas"]["AgentRunDetailOut"];
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
            /** @description `agent.run_not_found`. */
            404: {
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
    cancelAgentRun: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description UUID de la tarea en minúsculas. */
                run_id: string;
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
                    "application/json": components["schemas"]["AgentRunOut"];
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
            /** @description `agent.run_not_found`. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description `agent.not_cancellable`: ya terminó. */
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
    listAgents: {
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
                    "application/json": components["schemas"]["AgentListOut"];
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
    listSchedules: {
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
                    "application/json": components["schemas"]["ScheduleListOut"];
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
    createSchedule: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CreateScheduleIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ScheduleOut"];
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
            /** @description `agent.unknown` o `site.not_found`. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description `schedule.duplicate`. */
            409: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description `schedule.invalid` o `agent.site_required`. */
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
    deleteSchedule: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description UUID de la programación en minúsculas. */
                schedule_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            /** @description Successful Response */
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
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
            /** @description `schedule.not_found`. */
            404: {
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
    updateSchedule: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                /** @description UUID de la programación en minúsculas. */
                schedule_id: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["UpdateScheduleIn"];
            };
        };
        responses: {
            /** @description Successful Response */
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ScheduleOut"];
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
            /** @description `schedule.not_found`. */
            404: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ErrorOut"];
                };
            };
            /** @description `schedule.invalid`. */
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
