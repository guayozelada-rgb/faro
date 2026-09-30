<?php
/**
 * Pruebas generales de las rutas faro/v1.
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

/**
 * Registro, autorización, cabeceras, estado y revocación.
 */
class Test_Faro_Rest_Routes extends Faro_Test_Case {

	/**
	 * Rutas firmadas: método y ruta.
	 *
	 * @return array<int, array{0: string, 1: string}>
	 */
	private function signed_routes(): array {
		return array(
			array( 'GET', '/faro/v1/status' ),
			array( 'GET', '/faro/v1/pages' ),
			array( 'GET', '/faro/v1/posts' ),
			array( 'GET', '/faro/v1/products' ),
			array( 'DELETE', '/faro/v1/connection' ),
		);
	}

	public function test_expected_routes_are_registered(): void {
		$routes = rest_get_server()->get_routes( 'faro/v1' );

		foreach ( array( '/faro/v1/pair', '/faro/v1/status', '/faro/v1/pages', '/faro/v1/posts', '/faro/v1/products', '/faro/v1/connection' ) as $route ) {
			$this->assertArrayHasKey( $route, $routes );
		}
	}

	public function test_no_route_uses_return_true_or_lacks_permission_callback(): void {
		$routes = rest_get_server()->get_routes( 'faro/v1' );
		$this->assertNotEmpty( $routes );

		foreach ( $routes as $route => $handlers ) {
			if ( '/faro/v1' === $route ) {
				continue; // Índice del espacio de nombres (lo crea WordPress; lo usa el descubrimiento del motor).
			}
			foreach ( $handlers as $handler ) {
				$this->assertArrayHasKey( 'permission_callback', $handler, $route );
				$callback = $handler['permission_callback'];
				$this->assertIsCallable( $callback, $route );
				$this->assertNotSame( '__return_true', $callback, $route );
				$this->assertContains(
					$callback,
					array(
						array( Faro_Signature::class, 'permission' ),
						array( Faro_Pairing::class, 'permission' ),
					),
					$route
				);
			}
		}
	}

	public function test_all_signed_routes_without_signature_return_401(): void {
		$this->pair();

		foreach ( $this->signed_routes() as $route ) {
			$response = $this->dispatch( new WP_REST_Request( $route[0], $route[1] ) );
			$this->assertSame( 401, $response->get_status(), $route[1] );
			$this->assertSame( 'wp.invalid_signature', $this->error_code( $response ), $route[1] );
		}
		$this->assertNotNull( Faro_Connection::get(), 'DELETE sin firma no revoca.' );
	}

	public function test_unsigned_request_with_invalid_params_returns_401_not_400(): void {
		$this->pair();
		$request = new WP_REST_Request( 'GET', '/faro/v1/posts' );
		$request->set_query_params( array( 'per_page' => '500' ) );

		$response = $this->dispatch( $request );

		$this->assertSame( 401, $response->get_status() );
	}

	public function test_signed_request_with_invalid_params_returns_invalid_input(): void {
		$credentials = $this->pair();

		foreach ( array( array( 'per_page' => '101' ), array( 'per_page' => '0' ), array( 'page' => '0' ), array( 'page' => 'abc' ) ) as $query ) {
			$response = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/posts', $query ) );
			$this->assertSame( 400, $response->get_status(), (string) wp_json_encode( $query ) );
			$this->assertSame( 'wp.invalid_input', $this->error_code( $response ) );
		}
	}

	public function test_cache_control_no_store_on_every_response(): void {
		$credentials = $this->pair();

		$responses = array(
			$this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/status' ) ),
			$this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/posts' ) ),
			$this->dispatch( new WP_REST_Request( 'GET', '/faro/v1/status' ) ),
			$this->dispatch( new WP_REST_Request( 'GET', '/faro/v1' ) ),
			$this->pair_request( array( 'code' => '000000' ) ),
		);
		foreach ( $responses as $response ) {
			$this->assertSame( 'no-store, private', $response->get_headers()['Cache-Control'] ?? null );
		}

		$other = $this->dispatch( new WP_REST_Request( 'GET', '/' ) );
		$this->assertArrayNotHasKey( 'Cache-Control', $other->get_headers(), 'Solo se tocan las rutas faro/v1.' );
	}

	public function test_status_returns_site_information(): void {
		$credentials = $this->pair();
		self::factory()->post->create_many( 2, array( 'post_status' => 'publish' ) );
		self::factory()->post->create( array( 'post_status' => 'draft' ) );
		self::factory()->post->create(
			array(
				'post_type'   => 'page',
				'post_status' => 'publish',
			)
		);

		$response = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/status' ) );

		$this->assertSame( 200, $response->get_status() );
		$data = $response->get_data();
		$this->assertSame( 1, $data['api_version'] );
		$this->assertSame( FARO_VERSION, $data['plugin_version'] );
		$this->assertSame( get_bloginfo( 'version' ), $data['wp_version'] );
		$this->assertSame( untrailingslashit( home_url() ), $data['home_url'] );
		$this->assertIsString( $data['site_name'] );
		$this->assertSame( 'none', $data['seo_plugin'] );
		$this->assertSame( (int) wp_count_posts( 'post' )->publish, $data['counts']['posts'] );
		$this->assertSame( (int) wp_count_posts( 'page' )->publish, $data['counts']['pages'] );
		$this->assertSame( $credentials['connection_id'], $data['connection']['connection_id'] );
		$this->assertSame( '2026-09-21T14:13:20Z', $data['connection']['created_at'] );
		$this->assertSame( array( 'active', 'version', 'hpos_enabled' ), array_keys( $data['woocommerce'] ) );
		$this->assertStringNotContainsString( $credentials['token'], (string) wp_json_encode( $data ) );
	}

	public function test_delete_connection_revokes(): void {
		$credentials = $this->pair();

		$response = $this->dispatch( $this->signed_request( $credentials, 'DELETE', '/faro/v1/connection' ) );

		$this->assertSame( 200, $response->get_status() );
		$this->assertSame( array( 'revoked' => true ), $response->get_data() );
		$this->assertNull( Faro_Connection::get() );

		$after = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/status' ) );
		$this->assertSame( 401, $after->get_status() );
		$this->assertSame( 'wp.revoked', $this->error_code( $after ) );
	}

	public function test_error_messages_do_not_leak_signature_details(): void {
		$credentials = $this->pair();

		$response = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/status', array(), '', array( 'signature' => base64_encode( str_repeat( 'z', 32 ) ) ) ) ); // phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_encode -- Firma de prueba.

		$data = $response->get_data();
		$this->assertSame( array( 'status' => 401 ), $data['data'] );
		$this->assertSame( 'La conexión con Faro no es válida.', $data['message'] );
	}

	public function test_route_case_does_not_change_content_type(): void {
		$credentials = $this->pair();
		$page_id     = self::factory()->post->create(
			array(
				'post_type'   => 'page',
				'post_status' => 'publish',
			)
		);
		$post_id     = self::factory()->post->create( array( 'post_status' => 'publish' ) );

		$response = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/PAGES', array( 'per_page' => '100' ) ) );

		$this->assertSame( 200, $response->get_status() );
		$ids = wp_list_pluck( $response->get_data()['items'], 'id' );
		$this->assertContains( $page_id, $ids );
		$this->assertNotContains( $post_id, $ids, '/PAGES devuelve páginas, no entradas.' );
	}
}
