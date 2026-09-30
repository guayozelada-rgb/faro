<?php
/**
 * Errores de la API de Faro.
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

/**
 * Crea los WP_Error de la API con código estable, mensaje en español y estado HTTP.
 * Nunca incluye detalles de la firma ni datos internos.
 */
final class Faro_Errors {

	/**
	 * Crea un error de la API.
	 *
	 * @param string               $code  Código estable (por ejemplo "wp.invalid_signature").
	 * @param array<string, mixed> $extra Datos adicionales públicos (por ejemplo attempts_left).
	 * @return WP_Error
	 */
	public static function get( string $code, array $extra = array() ): WP_Error {
		$definitions = self::definitions();
		if ( ! isset( $definitions[ $code ] ) ) {
			$code = 'wp.invalid_input';
		}
		list( $status, $message ) = $definitions[ $code ];

		return new WP_Error( $code, $message, array_merge( array( 'status' => $status ), $extra ) );
	}

	/**
	 * Estado HTTP y mensaje de cada código.
	 *
	 * @return array<string, array{0: int, 1: string}>
	 */
	private static function definitions(): array {
		return array(
			'wp.invalid_input'     => array( 400, __( 'Los datos enviados no son válidos.', 'faro' ) ),
			'wp.invalid_signature' => array( 401, __( 'La conexión con Faro no es válida.', 'faro' ) ),
			'wp.revoked'           => array( 401, __( 'Este sitio ya no está conectado con Faro.', 'faro' ) ),
			'wp.connection_broken' => array( 401, __( 'La conexión con Faro dejó de funcionar porque cambiaron las claves de seguridad de WordPress.', 'faro' ) ),
			'wp.stale_request'     => array( 401, __( 'La hora de la petición no coincide con la del sitio.', 'faro' ) ),
			'wp.pairing_invalid'   => array( 403, __( 'El código no coincide.', 'faro' ) ),
			'wp.insecure_site'     => array( 403, __( 'Este sitio no usa HTTPS. Faro solo se conecta a sitios con HTTPS.', 'faro' ) ),
			'wp.pairing_expired'   => array( 410, __( 'El código caducó o ya se usó. Genera uno nuevo en WordPress.', 'faro' ) ),
			'wp.rate_limited'      => array( 429, __( 'Hubo demasiados intentos. Espera 15 minutos e intenta de nuevo.', 'faro' ) ),
		);
	}
}
