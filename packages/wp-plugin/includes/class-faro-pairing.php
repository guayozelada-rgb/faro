<?php
/**
 * Vinculación con código de 6 dígitos.
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

/**
 * Genera y canjea el código de vinculación: un solo uso, 10 minutos, 5 intentos
 * y un límite de 10 fallos cada 15 minutos por dirección IP.
 */
final class Faro_Pairing {

	/**
	 * Opción con el código pendiente.
	 */
	public const OPTION = 'faro_pairing';

	/**
	 * Vida del código (segundos).
	 */
	public const CODE_TTL = 600;

	/**
	 * Intentos por código.
	 */
	public const ATTEMPTS = 5;

	/**
	 * Fallos permitidos por IP en la ventana.
	 */
	public const IP_MAX_FAILURES = 10;

	/**
	 * Ventana del límite por IP (segundos).
	 */
	public const IP_WINDOW = 900;

	/**
	 * Prefijo del transient del límite por IP.
	 */
	public const IP_TRANSIENT_PREFIX = 'faro_pair_ip_';

	/**
	 * Tipo de entorno forzado (solo pruebas); null usa wp_get_environment_type().
	 *
	 * @var string|null
	 */
	private static ?string $environment_type = null;

	/**
	 * Genera un código nuevo (invalida el anterior) y lo devuelve en claro.
	 * Solo se guarda su HMAC con wp_salt( 'auth' ).
	 *
	 * @return string Código de 6 dígitos, con ceros a la izquierda.
	 */
	public static function create_code(): string {
		$code = str_pad( (string) random_int( 0, 999999 ), 6, '0', STR_PAD_LEFT );
		self::save(
			array(
				'code_hash'     => self::hash_code( $code ),
				'expires_at'    => Faro_Clock::now() + self::CODE_TTL,
				'attempts_left' => self::ATTEMPTS,
			)
		);

		return $code;
	}

	/**
	 * HMAC del código con wp_salt( 'auth' ).
	 *
	 * @param string $code Código.
	 * @return string
	 */
	public static function hash_code( string $code ): string {
		return hash_hmac( 'sha256', $code, wp_salt( 'auth' ) );
	}

	/**
	 * Código pendiente y vigente, o null. Borra el caducado.
	 *
	 * @return array{code_hash: string, expires_at: int, attempts_left: int}|null
	 */
	public static function get_pending(): ?array {
		$pending = get_option( self::OPTION );
		if ( ! is_array( $pending )
			|| ! isset( $pending['code_hash'], $pending['expires_at'], $pending['attempts_left'] )
			|| ! is_string( $pending['code_hash'] )
			|| ! is_int( $pending['expires_at'] )
			|| ! is_int( $pending['attempts_left'] ) ) {
			if ( false !== $pending ) {
				delete_option( self::OPTION );
			}
			return null;
		}
		if ( $pending['expires_at'] <= Faro_Clock::now() || $pending['attempts_left'] <= 0 ) {
			delete_option( self::OPTION );
			return null;
		}

		return array(
			'code_hash'     => $pending['code_hash'],
			'expires_at'    => $pending['expires_at'],
			'attempts_left' => $pending['attempts_left'],
		);
	}

	/**
	 * Indica si el sitio permite vincular: HTTPS o entorno "local".
	 *
	 * @return bool
	 */
	public static function site_allows_pairing(): bool {
		return wp_is_using_https() || 'local' === ( self::$environment_type ?? wp_get_environment_type() );
	}

	/**
	 * Fuerza el tipo de entorno (solo pruebas). Con null vuelve al real.
	 *
	 * @param string|null $type Tipo de entorno.
	 * @return void
	 */
	public static function set_environment_type( ?string $type ): void {
		self::$environment_type = $type;
	}

	/**
	 * Permiso de POST /faro/v1/pair: sitio seguro, IP sin bloquear y código pendiente.
	 *
	 * @return bool|WP_Error
	 */
	public static function permission() {
		if ( ! self::site_allows_pairing() ) {
			return Faro_Errors::get( 'wp.insecure_site' );
		}
		if ( self::ip_is_limited() ) {
			return Faro_Errors::get( 'wp.rate_limited' );
		}
		if ( null === self::get_pending() ) {
			return Faro_Errors::get( 'wp.pairing_expired' );
		}

		return true;
	}

	/**
	 * Canjea un código. Con éxito lo borra (un solo uso); con fallo resta un intento.
	 *
	 * @param string $code Código recibido.
	 * @return true|WP_Error
	 */
	public static function redeem( string $code ) {
		$pending = self::get_pending();
		if ( null === $pending ) {
			return Faro_Errors::get( 'wp.pairing_expired' );
		}

		// Se descuenta el intento antes de comparar para acortar la ventana de peticiones simultáneas.
		--$pending['attempts_left'];
		self::save( $pending );

		if ( hash_equals( $pending['code_hash'], self::hash_code( $code ) ) ) {
			delete_option( self::OPTION );
			return true;
		}

		self::record_ip_failure();
		if ( $pending['attempts_left'] <= 0 ) {
			delete_option( self::OPTION );
		}

		return Faro_Errors::get( 'wp.pairing_invalid', array( 'attempts_left' => max( 0, $pending['attempts_left'] ) ) );
	}

	/**
	 * Indica si la IP actual superó el límite de fallos.
	 *
	 * @return bool
	 */
	public static function ip_is_limited(): bool {
		$state = self::ip_state();

		return null !== $state && $state['count'] >= self::IP_MAX_FAILURES;
	}

	/**
	 * Suma un fallo a la IP actual.
	 *
	 * @return void
	 */
	private static function record_ip_failure(): void {
		$now   = Faro_Clock::now();
		$state = self::ip_state() ?? array(
			'count' => 0,
			'until' => $now + self::IP_WINDOW,
		);
		++$state['count'];
		set_transient( self::ip_transient(), $state, max( 1, $state['until'] - $now ) );
	}

	/**
	 * Estado del límite de la IP actual, o null si no hay fallos vigentes.
	 *
	 * @return array{count: int, until: int}|null
	 */
	private static function ip_state(): ?array {
		$state = get_transient( self::ip_transient() );
		if ( ! is_array( $state ) || ! isset( $state['count'], $state['until'] ) || ! is_int( $state['count'] ) || ! is_int( $state['until'] ) ) {
			return null;
		}
		if ( $state['until'] <= Faro_Clock::now() ) {
			return null;
		}

		return array(
			'count' => $state['count'],
			'until' => $state['until'],
		);
	}

	/**
	 * Nombre del transient de la IP actual. Solo REMOTE_ADDR (no se confía en X-Forwarded-For).
	 *
	 * @return string
	 */
	private static function ip_transient(): string {
		$ip = isset( $_SERVER['REMOTE_ADDR'] ) ? sanitize_text_field( wp_unslash( $_SERVER['REMOTE_ADDR'] ) ) : '';

		return self::IP_TRANSIENT_PREFIX . hash( 'sha256', $ip );
	}

	/**
	 * Guarda el código pendiente (autoload = no).
	 *
	 * @param array{code_hash: string, expires_at: int, attempts_left: int} $pending Código pendiente.
	 * @return void
	 */
	private static function save( array $pending ): void {
		delete_option( self::OPTION );
		add_option( self::OPTION, $pending, '', false );
	}
}
