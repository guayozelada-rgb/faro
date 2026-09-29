<?php
/**
 * Firma HMAC v1 de las peticiones de la app (ADR 0011 §2).
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

/**
 * Canónica:
 *   METHOD "\n" ROUTE "\n" TIMESTAMP "\n" NONCE "\n" hex( sha256( raw_body ) )
 * Firma:
 *   base64( HMAC-SHA256( clave = 32 bytes de base64url_decode( hmac_secret ), canónica ) )
 *
 * ROUTE es la ruta REST sin barra final y, si hay parámetros de consulta distintos de
 * rest_route, "?" + pares rawurlencode( clave ) "=" rawurlencode( valor ) ordenados por
 * clave codificada y luego por valor codificado (orden de bytes), unidos con "&".
 */
final class Faro_Signature {

	/**
	 * Ventana de validez de la hora (segundos, en ambos sentidos).
	 */
	public const WINDOW = 300;

	/**
	 * Vida de un nonce guardado (segundos).
	 */
	public const NONCE_TTL = 600;

	/**
	 * Prefijo de los transients de nonces usados.
	 */
	public const NONCE_TRANSIENT_PREFIX = 'faro_nonce_';

	/**
	 * Ruta canónica con su consulta ordenada. Null si la consulta tiene valores no escalares.
	 *
	 * @param string              $route Ruta REST, por ejemplo "/faro/v1/posts".
	 * @param array<mixed, mixed> $query Parámetros de consulta ya decodificados.
	 * @return string|null
	 */
	public static function canonical_route( string $route, array $query ): ?string {
		$route = untrailingslashit( $route );
		$pairs = array();
		foreach ( $query as $key => $value ) {
			if ( 'rest_route' === $key ) {
				continue;
			}
			if ( ! is_scalar( $value ) ) {
				return null;
			}
			if ( is_bool( $value ) ) {
				$value = $value ? '1' : '';
			}
			$pairs[] = array( rawurlencode( (string) $key ), rawurlencode( (string) $value ) );
		}
		if ( array() === $pairs ) {
			return $route;
		}
		usort(
			$pairs,
			static function ( array $a, array $b ): int {
				$by_key = strcmp( $a[0], $b[0] );
				return 0 !== $by_key ? $by_key : strcmp( $a[1], $b[1] );
			}
		);

		return $route . '?' . implode(
			'&',
			array_map(
				static function ( array $pair ): string {
					return $pair[0] . '=' . $pair[1];
				},
				$pairs
			)
		);
	}

	/**
	 * Texto canónico que se firma.
	 *
	 * @param string $method          Método HTTP.
	 * @param string $canonical_route Resultado de canonical_route().
	 * @param string $timestamp       Hora Unix en decimal.
	 * @param string $nonce           Nonce base64url.
	 * @param string $body            Cuerpo crudo.
	 * @return string
	 */
	public static function canonical( string $method, string $canonical_route, string $timestamp, string $nonce, string $body ): string {
		return implode( "\n", array( strtoupper( $method ), $canonical_route, $timestamp, $nonce, hash( 'sha256', $body ) ) );
	}

	/**
	 * Firma un texto canónico.
	 *
	 * @param string $canonical Texto canónico.
	 * @param string $key       Clave HMAC (32 bytes).
	 * @return string Base64 estándar.
	 */
	public static function sign( string $canonical, string $key ): string {
		return base64_encode( hash_hmac( 'sha256', $canonical, $key, true ) ); // phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_encode -- Codificación de la firma, no ofuscación.
	}

	/**
	 * Permiso de las rutas firmadas. Actualiza last_seen_at si la firma es válida.
	 *
	 * @param WP_REST_Request $request Petición.
	 * @return bool|WP_Error
	 *
	 * @phpstan-param WP_REST_Request<array<string, mixed>> $request
	 */
	public static function permission( WP_REST_Request $request ) {
		$result = self::verify( $request );
		if ( is_wp_error( $result ) ) {
			return $result;
		}
		$connection = Faro_Connection::get();
		if ( null !== $connection ) {
			Faro_Connection::touch( $connection );
		}

		return true;
	}

	/**
	 * Verifica la firma en el orden de ADR 0011 §2. El nonce se guarda solo tras una firma válida.
	 *
	 * @param WP_REST_Request $request Petición.
	 * @return bool|WP_Error
	 *
	 * @phpstan-param WP_REST_Request<array<string, mixed>> $request
	 */
	public static function verify( WP_REST_Request $request ) {
		$connection_id = (string) $request->get_header( 'x_faro_connection' );
		$token         = (string) $request->get_header( 'x_faro_token' );
		$timestamp     = (string) $request->get_header( 'x_faro_timestamp' );
		$nonce         = (string) $request->get_header( 'x_faro_nonce' );
		$signature     = (string) $request->get_header( 'x_faro_signature' );

		// 1. Cabeceras presentes y con la forma esperada.
		if ( 1 !== preg_match( '/^[0-9a-f-]{36}$/', $connection_id )
			|| 1 !== preg_match( '/^[A-Za-z0-9_-]{43}$/', $token )
			|| 1 !== preg_match( '/^[0-9]{1,12}$/', $timestamp )
			|| 1 !== preg_match( '/^[A-Za-z0-9_-]{22}$/', $nonce )
			|| 1 !== preg_match( '#^[A-Za-z0-9+/]{43}=$#', $signature ) ) {
			return Faro_Errors::get( 'wp.invalid_signature' );
		}

		// 2. La conexión existe y es esta.
		$connection = Faro_Connection::get();
		if ( null === $connection || ! hash_equals( $connection['connection_id'], $connection_id ) ) {
			return Faro_Errors::get( 'wp.revoked' );
		}

		// 3. El secreto se puede descifrar.
		$secret = Faro_Connection::secret( $connection );
		if ( null === $secret ) {
			return Faro_Errors::get( 'wp.connection_broken' );
		}

		// 4. El token coincide.
		if ( ! hash_equals( $connection['token_sha256'], hash( 'sha256', $token ) ) ) {
			return Faro_Errors::get( 'wp.invalid_signature' );
		}

		// 5. Hora dentro de la ventana.
		if ( abs( Faro_Clock::now() - (int) $timestamp ) > self::WINDOW ) {
			return Faro_Errors::get( 'wp.stale_request' );
		}

		// 6. Firma HMAC.
		$route = self::canonical_route( $request->get_route(), $request->get_query_params() );
		if ( null === $route ) {
			return Faro_Errors::get( 'wp.invalid_signature' );
		}
		$canonical = self::canonical( $request->get_method(), $route, $timestamp, $nonce, (string) $request->get_body() );
		if ( ! hash_equals( self::sign( $canonical, $secret ), $signature ) ) {
			return Faro_Errors::get( 'wp.invalid_signature' );
		}

		// 7. Nonce no usado; solo entonces se guarda.
		$nonce_key = self::NONCE_TRANSIENT_PREFIX . hash( 'sha256', $nonce );
		if ( false !== get_transient( $nonce_key ) ) {
			return Faro_Errors::get( 'wp.invalid_signature' );
		}
		set_transient( $nonce_key, 1, self::NONCE_TTL );

		return true;
	}
}
