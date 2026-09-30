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
 *
 * El código pendiente se modifica solo con operaciones atómicas sobre la tabla de opciones
 * (comparar e intercambiar): peticiones en paralelo no pueden sumar intentos ni revivir
 * un código ya canjeado.
 *
 * @phpstan-type FaroPending array{code_hash: string, expires_at: int, attempts_left: int}
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
	 * Reintentos de comparar e intercambiar ante escrituras simultáneas.
	 */
	private const CAS_RETRIES = 10;

	/**
	 * Tipo de entorno forzado. Solo tiene efecto en las pruebas (constante FARO_TESTING).
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
		delete_option( self::OPTION );
		add_option(
			self::OPTION,
			array(
				'code_hash'     => self::hash_code( $code ),
				'expires_at'    => Faro_Clock::now() + self::CODE_TTL,
				'attempts_left' => self::ATTEMPTS,
			),
			'',
			false
		);
		self::flush_cache();

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
	 * Código pendiente y vigente, o null. Borra el caducado o agotado.
	 * Lee siempre de la base (no de la caché de objetos).
	 *
	 * @return array{code_hash: string, expires_at: int, attempts_left: int}|null
	 */
	public static function get_pending(): ?array {
		$raw = self::read_raw();
		if ( null === $raw ) {
			return null;
		}
		$pending = self::parse( $raw );
		if ( null === $pending || $pending['expires_at'] <= Faro_Clock::now() || $pending['attempts_left'] <= 0 ) {
			self::delete_if_unchanged( $raw );
			return null;
		}

		return $pending;
	}

	/**
	 * Indica si el sitio permite generar códigos: HTTPS configurado o entorno "local".
	 *
	 * @return bool
	 */
	public static function site_allows_pairing(): bool {
		return wp_is_using_https() || self::is_local();
	}

	/**
	 * Indica si la petición actual puede vincular: sitio con HTTPS y petición recibida por HTTPS
	 * (is_ssl()), o entorno "local". No se confía en X-Forwarded-Proto: detrás de un proxy que
	 * termina TLS, el administrador debe configurar WordPress para que is_ssl() detecte HTTPS.
	 *
	 * @return bool
	 */
	public static function request_allows_pairing(): bool {
		return ( wp_is_using_https() && is_ssl() ) || self::is_local();
	}

	/**
	 * Fuerza el tipo de entorno. Solo para pruebas: sin la constante FARO_TESTING no tiene efecto.
	 *
	 * @param string|null $type Tipo de entorno, o null para usar el real.
	 * @return void
	 */
	public static function set_environment_type( ?string $type ): void {
		self::$environment_type = $type;
	}

	/**
	 * Permiso de POST /faro/v1/pair: petición segura, IP sin bloquear y código pendiente.
	 * Las consultas sin código pendiente también cuentan para el límite por IP.
	 * Se evalúa una sola vez por petición: WordPress vuelve a llamar a los permisos para la
	 * cabecera Allow (rest_send_allow_header) y el fallo no debe contarse dos veces.
	 *
	 * @param WP_REST_Request $request Petición.
	 * @return bool|WP_Error
	 *
	 * @phpstan-param WP_REST_Request<array<string, mixed>> $request
	 */
	public static function permission( WP_REST_Request $request ) {
		return Faro_Rest::once( $request, 'pairing', array( self::class, 'check_permission' ) );
	}

	/**
	 * Comprobaciones del permiso de /pair (ver permission()).
	 *
	 * @return bool|WP_Error
	 */
	public static function check_permission() {
		if ( ! self::request_allows_pairing() ) {
			return Faro_Errors::get( 'wp.insecure_site' );
		}
		if ( self::ip_is_limited() ) {
			return Faro_Errors::get( 'wp.rate_limited' );
		}
		if ( null === self::get_pending() ) {
			self::record_ip_failure();
			return Faro_Errors::get( 'wp.pairing_expired' );
		}

		return true;
	}

	/**
	 * Canjea un código. Descuenta un intento de forma atómica y solo entonces compara;
	 * con acierto borra el código de forma atómica (un único ganador).
	 *
	 * @param string $code Código recibido.
	 * @return true|WP_Error
	 */
	public static function redeem( string $code ) {
		$reserved = self::reserve_attempt();
		if ( null === $reserved ) {
			return Faro_Errors::get( 'wp.pairing_expired' );
		}
		list( $pending, $raw ) = $reserved;

		if ( hash_equals( $pending['code_hash'], self::hash_code( $code ) ) ) {
			return self::consume( $pending['code_hash'] ) ? true : Faro_Errors::get( 'wp.pairing_expired' );
		}

		self::record_ip_failure();
		if ( $pending['attempts_left'] <= 0 ) {
			self::delete_if_unchanged( $raw );
		}

		return Faro_Errors::get( 'wp.pairing_invalid', array( 'attempts_left' => max( 0, $pending['attempts_left'] ) ) );
	}

	/**
	 * Descuenta un intento con comparar e intercambiar. Devuelve el estado ya descontado y su
	 * valor guardado, o null si no hay código vigente.
	 *
	 * @return array{0: array{code_hash: string, expires_at: int, attempts_left: int}, 1: string}|null
	 */
	private static function reserve_attempt(): ?array {
		for ( $i = 0; $i < self::CAS_RETRIES; $i++ ) {
			$raw = self::read_raw();
			if ( null === $raw ) {
				return null;
			}
			$pending = self::parse( $raw );
			if ( null === $pending || $pending['expires_at'] <= Faro_Clock::now() || $pending['attempts_left'] <= 0 ) {
				self::delete_if_unchanged( $raw );
				return null;
			}
			--$pending['attempts_left'];
			$new_raw = maybe_serialize( $pending );
			if ( self::compare_and_swap( $raw, $new_raw ) ) {
				return array( $pending, $new_raw );
			}
		}

		return null;
	}

	/**
	 * Borra el código tras un acierto, solo si sigue siendo el mismo código (un único ganador).
	 *
	 * @param string $code_hash HMAC del código acertado.
	 * @return bool
	 */
	private static function consume( string $code_hash ): bool {
		for ( $i = 0; $i < self::CAS_RETRIES; $i++ ) {
			$raw = self::read_raw();
			if ( null === $raw ) {
				return false;
			}
			$pending = self::parse( $raw );
			if ( null === $pending || ! hash_equals( $pending['code_hash'], $code_hash ) ) {
				return false;
			}
			if ( self::delete_if_unchanged( $raw ) ) {
				return true;
			}
		}

		return false;
	}

	/**
	 * Reemplaza el valor guardado solo si no cambió desde que se leyó.
	 *
	 * @internal Público solo para las pruebas de concurrencia.
	 *
	 * @param string $expected_raw Valor leído.
	 * @param string $new_raw      Valor nuevo.
	 * @return bool true si esta petición hizo el cambio.
	 */
	public static function compare_and_swap( string $expected_raw, string $new_raw ): bool {
		global $wpdb;
		// phpcs:ignore WordPress.DB.DirectDatabaseQuery.DirectQuery, WordPress.DB.DirectDatabaseQuery.NoCaching -- Comparar e intercambiar atómico; la caché se invalida abajo.
		$rows = $wpdb->query(
			$wpdb->prepare(
				"UPDATE {$wpdb->options} SET option_value = %s WHERE option_name = %s AND option_value = %s",
				$new_raw,
				self::OPTION,
				$expected_raw
			)
		);
		self::flush_cache();

		return 1 === $rows;
	}

	/**
	 * Borra el código solo si su valor no cambió desde que se leyó.
	 *
	 * @param string $expected_raw Valor leído.
	 * @return bool true si esta petición lo borró.
	 */
	private static function delete_if_unchanged( string $expected_raw ): bool {
		global $wpdb;
		// phpcs:ignore WordPress.DB.DirectDatabaseQuery.DirectQuery, WordPress.DB.DirectDatabaseQuery.NoCaching -- Borrado condicional atómico; la caché se invalida abajo.
		$rows = $wpdb->query(
			$wpdb->prepare(
				"DELETE FROM {$wpdb->options} WHERE option_name = %s AND option_value = %s",
				self::OPTION,
				$expected_raw
			)
		);
		self::flush_cache();

		return 1 === $rows;
	}

	/**
	 * Valor guardado del código tal como está en la base, o null.
	 *
	 * @internal Público solo para las pruebas de concurrencia.
	 *
	 * @return string|null
	 */
	public static function read_raw(): ?string {
		global $wpdb;
		// phpcs:ignore WordPress.DB.DirectDatabaseQuery.DirectQuery, WordPress.DB.DirectDatabaseQuery.NoCaching -- Lectura para comparar e intercambiar; no debe venir de la caché.
		$raw = $wpdb->get_var( $wpdb->prepare( "SELECT option_value FROM {$wpdb->options} WHERE option_name = %s", self::OPTION ) );

		return is_string( $raw ) ? $raw : null;
	}

	/**
	 * Interpreta el valor guardado.
	 *
	 * @param string $raw Valor guardado.
	 * @return array{code_hash: string, expires_at: int, attempts_left: int}|null
	 */
	private static function parse( string $raw ): ?array {
		$pending = maybe_unserialize( $raw );
		if ( ! is_array( $pending )
			|| ! isset( $pending['code_hash'], $pending['expires_at'], $pending['attempts_left'] )
			|| ! is_string( $pending['code_hash'] )
			|| ! is_int( $pending['expires_at'] )
			|| ! is_int( $pending['attempts_left'] ) ) {
			return null;
		}

		return array(
			'code_hash'     => $pending['code_hash'],
			'expires_at'    => $pending['expires_at'],
			'attempts_left' => $pending['attempts_left'],
		);
	}

	/**
	 * Invalida la caché de objetos de la opción.
	 *
	 * @return void
	 */
	private static function flush_cache(): void {
		wp_cache_delete( self::OPTION, 'options' );
		wp_cache_delete( 'notoptions', 'options' );
		wp_cache_delete( 'alloptions', 'options' );
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
	 * Suma un fallo a la IP actual. La lectura y escritura van dentro de un candado de la base
	 * (GET_LOCK) para que fallos simultáneos no se pierdan.
	 *
	 * @return void
	 */
	public static function record_ip_failure(): void {
		global $wpdb;
		$lock = 'faro_pair_ip_' . substr( hash( 'sha256', self::ip_transient() ), 0, 32 );
		// phpcs:ignore WordPress.DB.DirectDatabaseQuery.DirectQuery, WordPress.DB.DirectDatabaseQuery.NoCaching -- Candado de la base para el contador.
		$locked = '1' === (string) $wpdb->get_var( $wpdb->prepare( 'SELECT GET_LOCK(%s, 2)', $lock ) );
		try {
			$now   = Faro_Clock::now();
			$state = self::ip_state() ?? array(
				'count' => 0,
				'until' => $now + self::IP_WINDOW,
			);
			++$state['count'];
			set_transient( self::ip_transient(), $state, max( 1, $state['until'] - $now ) );
		} finally {
			if ( $locked ) {
				// phpcs:ignore WordPress.DB.DirectDatabaseQuery.DirectQuery, WordPress.DB.DirectDatabaseQuery.NoCaching -- Libera el candado.
				$wpdb->query( $wpdb->prepare( 'SELECT RELEASE_LOCK(%s)', $lock ) );
			}
		}
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
	 * Nombre del transient de la IP actual. Solo REMOTE_ADDR: no se confía en X-Forwarded-For
	 * (detrás de un CDN o proxy, todas las peticiones comparten la IP del proxy).
	 *
	 * @return string
	 */
	private static function ip_transient(): string {
		$ip = isset( $_SERVER['REMOTE_ADDR'] ) ? sanitize_text_field( wp_unslash( $_SERVER['REMOTE_ADDR'] ) ) : '';

		return self::IP_TRANSIENT_PREFIX . hash( 'sha256', $ip );
	}

	/**
	 * Indica si el entorno es "local".
	 *
	 * @return bool
	 */
	private static function is_local(): bool {
		$type = wp_get_environment_type();
		if ( null !== self::$environment_type && defined( 'FARO_TESTING' ) && true === constant( 'FARO_TESTING' ) ) {
			$type = self::$environment_type;
		}

		return 'local' === $type;
	}
}
