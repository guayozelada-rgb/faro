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
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
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
}
