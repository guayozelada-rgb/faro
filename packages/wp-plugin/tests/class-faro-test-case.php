<?php
/**
 * Caso base de las pruebas del plugin.
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

/**
 * Utilidades comunes: reloj fijo, vinculación y peticiones firmadas.
 */
abstract class Faro_Test_Case extends WP_UnitTestCase {

	/**
	 * Hora fija de las pruebas.
	 */
	public const NOW = 1790000000;

	/**
	 * Prepara cada prueba.
	 *
	 * @return void
	 */
	public function set_up(): void {
		parent::set_up();
		Faro_Clock::freeze( self::NOW );
		Faro_Pairing::set_environment_type( 'local' );
		Faro_Admin::reset();
		$_SERVER['REMOTE_ADDR'] = '203.0.113.10';
		delete_option( Faro_Connection::OPTION );
		delete_option( Faro_Pairing::OPTION );
		$GLOBALS['wp_rest_server'] = null; // phpcs:ignore WordPress.WP.GlobalVariablesOverride.Prohibited -- Servidor REST limpio en cada prueba.
	}

	/**
	 * Limpia después de cada prueba.
	 *
	 * @return void
	 */
	public function tear_down(): void {
		Faro_Clock::freeze( null );
		Faro_Pairing::set_environment_type( null );
		Faro_Admin::reset();
		remove_all_filters( 'salt' );
		$_POST    = array();
		$_REQUEST = array();
		parent::tear_down();
	}

	/**
	 * Fuerza el esquema de home_url() y site_url() (wp-env define WP_HOME y WP_SITEURL como constantes).
	 *
	 * @param string $scheme "http" o "https".
	 * @return void
	 */
	protected function force_scheme( string $scheme ): void {
		$filter = static function ( $url ) use ( $scheme ) {
			return set_url_scheme( (string) $url, $scheme );
		};
		add_filter( 'home_url', $filter );
		add_filter( 'site_url', $filter );
	}

	/**
	 * Ejecuta una petición como lo hace WordPress (incluye rest_post_dispatch).
	 *
	 * @param WP_REST_Request $request Petición.
	 * @return WP_REST_Response
	 */
	protected function dispatch( WP_REST_Request $request ): WP_REST_Response {
		$server   = rest_get_server();
		$response = $server->dispatch( $request );
		if ( is_wp_error( $response ) ) {
			$response = rest_convert_error_to_response( $response );
		}
		$response = rest_ensure_response( $response );

		return apply_filters( 'rest_post_dispatch', $response, $server, $request );
	}

	/**
	 * Código de error de una respuesta.
	 *
	 * @param WP_REST_Response $response Respuesta.
	 * @return string|null
	 */
	protected function error_code( WP_REST_Response $response ): ?string {
		$data = $response->get_data();

		return is_array( $data ) && isset( $data['code'] ) ? (string) $data['code'] : null;
	}

	/**
	 * Petición POST /pair.
	 *
	 * @param array<string, mixed> $body Cuerpo.
	 * @return WP_REST_Response
	 */
	protected function pair_request( array $body ): WP_REST_Response {
		$request = new WP_REST_Request( 'POST', '/faro/v1/pair' );
		$request->set_header( 'Content-Type', 'application/json' );
		$request->set_body( (string) wp_json_encode( $body ) );

		return $this->dispatch( $request );
	}

	/**
	 * Cuerpo válido de /pair.
	 *
	 * @param string $code Código.
	 * @return array<string, string>
	 */
	protected function pair_body( string $code ): array {
		return array(
			'code'            => $code,
			'app_instance_id' => '0192a6f0-7c1e-7b3a-9f00-000000000001',
			'app_version'     => '0.1.0',
		);
	}

	/**
	 * Vincula y devuelve las credenciales.
	 *
	 * @return array{connection_id: string, token: string, hmac_secret: string}
	 */
	protected function pair(): array {
		$response = $this->pair_request( $this->pair_body( Faro_Pairing::create_code() ) );
		$this->assertSame( 200, $response->get_status() );
		$data = $response->get_data();

		return array(
			'connection_id' => $data['connection_id'],
			'token'         => $data['token'],
			'hmac_secret'   => $data['hmac_secret'],
		);
	}

	/**
	 * Construye una petición firmada con una implementación independiente de la del plugin.
	 *
	 * @param array{connection_id: string, token: string, hmac_secret: string} $credentials Credenciales.
	 * @param string                                                           $method      Método.
	 * @param string                                                           $route       Ruta REST.
	 * @param array<string, string>                                            $query       Consulta.
	 * @param string                                                           $body        Cuerpo.
	 * @param array<string, string|int>                                        $overrides   Cambios en lo firmado o en las cabeceras.
	 * @return WP_REST_Request
	 */
	protected function signed_request( array $credentials, string $method, string $route, array $query = array(), string $body = '', array $overrides = array() ): WP_REST_Request {
		$timestamp = (string) ( $overrides['timestamp'] ?? Faro_Clock::now() );
		$nonce     = (string) ( $overrides['nonce'] ?? rtrim( strtr( base64_encode( random_bytes( 16 ) ), '+/', '-_' ), '=' ) ); // phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_encode -- Nonce de prueba.

		$sign_query = $query;
		ksort( $sign_query, SORT_STRING );
		$pairs = array();
		foreach ( $sign_query as $key => $value ) {
			$pairs[] = rawurlencode( (string) $key ) . '=' . rawurlencode( $value );
		}
		$sign_route = (string) ( $overrides['sign_route'] ?? ( $route . ( array() !== $pairs ? '?' . implode( '&', $pairs ) : '' ) ) );
		$sign_body  = (string) ( $overrides['sign_body'] ?? $body );
		$canonical  = implode(
			"\n",
			array(
				(string) ( $overrides['sign_method'] ?? $method ),
				$sign_route,
				(string) ( $overrides['sign_timestamp'] ?? $timestamp ),
				(string) ( $overrides['sign_nonce'] ?? $nonce ),
				hash( 'sha256', $sign_body ),
			)
		);
		$key        = base64_decode( strtr( $credentials['hmac_secret'], '-_', '+/' ) . '=' ); // phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_decode -- Clave de prueba.
		$signature  = base64_encode( hash_hmac( 'sha256', $canonical, (string) $key, true ) ); // phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_encode -- Firma de prueba.

		$request = new WP_REST_Request( $method, $route );
		$request->set_query_params( $query );
		if ( '' !== $body ) {
			$request->set_body( $body );
		}
		$headers = array(
			'X-Faro-Connection' => (string) ( $overrides['connection_id'] ?? $credentials['connection_id'] ),
			'X-Faro-Token'      => (string) ( $overrides['token'] ?? $credentials['token'] ),
			'X-Faro-Timestamp'  => $timestamp,
			'X-Faro-Nonce'      => $nonce,
			'X-Faro-Signature'  => (string) ( $overrides['signature'] ?? $signature ),
		);
		foreach ( $headers as $name => $value ) {
			if ( isset( $overrides[ 'omit_' . $name ] ) ) {
				continue;
			}
			$request->set_header( $name, $value );
		}

		return $request;
	}
}
