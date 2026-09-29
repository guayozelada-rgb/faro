<?php
/**
 * Pruebas de la firma HMAC v1.
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

/**
 * Faro_Signature y rutas firmadas.
 */
class Test_Faro_Signature extends Faro_Test_Case {

	/**
	 * Ruta de los vectores compartidos.
	 *
	 * @return string
	 */
	private function vectors_path(): string {
		$candidates = array(
			(string) getenv( 'FARO_SIGNATURE_VECTORS' ),
			ABSPATH . 'faro-fixtures/wp-signature-v1.json',
			dirname( __DIR__, 2 ) . '/shared/fixtures/wp-signature-v1.json',
		);
		foreach ( $candidates as $path ) {
			if ( '' !== $path && file_exists( $path ) ) {
				return $path;
			}
		}
		$this->fail( 'No se encontraron los vectores compartidos packages/shared/fixtures/wp-signature-v1.json.' );
	}

	public function test_shared_vectors(): void {
		$vectors = json_decode( (string) file_get_contents( $this->vectors_path() ), true ); // phpcs:ignore WordPress.WP.AlternativeFunctions.file_get_contents_file_get_contents -- Archivo local de pruebas.
		$this->assertIsArray( $vectors );
		$this->assertSame( 1, $vectors['version'] );
		$this->assertGreaterThanOrEqual( 8, count( $vectors['cases'] ) );

		foreach ( $vectors['cases'] as $case ) {
			$route = Faro_Signature::canonical_route( $case['route'], $case['query'] );
			$this->assertSame( $case['expected_canonical_route'], $route, $case['name'] );
			$canonical = Faro_Signature::canonical( $case['method'], (string) $route, $case['timestamp'], $case['nonce'], $case['body'] );
			$this->assertSame( $case['expected_canonical'], $canonical, $case['name'] );
			$key = Faro_Crypto::base64url_decode( $case['hmac_secret'] );
			$this->assertIsString( $key );
			$this->assertSame( 32, strlen( $key ), $case['name'] );
			$this->assertSame( $case['expected_signature'], Faro_Signature::sign( $canonical, $key ), $case['name'] );
		}
	}

	public function test_shared_vectors_verify_end_to_end(): void {
		$vectors = json_decode( (string) file_get_contents( $this->vectors_path() ), true ); // phpcs:ignore WordPress.WP.AlternativeFunctions.file_get_contents_file_get_contents -- Archivo local de pruebas.
		$case    = $vectors['cases'][1];
		$token   = 'test-token-not-real-000000000000000000000000';
		$token   = substr( $token, 0, 43 );
		update_option(
			Faro_Connection::OPTION,
			array(
				'connection_id'   => '6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b',
				'token_sha256'    => hash( 'sha256', $token ),
				'hmac_secret'     => Faro_Crypto::encrypt( (string) Faro_Crypto::base64url_decode( $case['hmac_secret'] ) ),
				'app_instance_id' => '0192a6f0-7c1e-7b3a-9f00-000000000001',
				'app_version'     => '0.1.0',
				'created_at'      => self::NOW,
				'last_seen_at'    => self::NOW,
			),
			false
		);
		Faro_Clock::freeze( (int) $case['timestamp'] );

		$request = new WP_REST_Request( $case['method'], $case['route'] );
		$request->set_query_params( $case['query'] );
		$request->set_header( 'X-Faro-Connection', '6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b' );
		$request->set_header( 'X-Faro-Token', $token );
		$request->set_header( 'X-Faro-Timestamp', $case['timestamp'] );
		$request->set_header( 'X-Faro-Nonce', $case['nonce'] );
		$request->set_header( 'X-Faro-Signature', $case['expected_signature'] );

		$this->assertTrue( Faro_Signature::verify( $request ) );
	}

	public function test_valid_signature_is_accepted(): void {
		$credentials = $this->pair();

		$response = $this->dispatch(
			$this->signed_request(
				$credentials,
				'GET',
				'/faro/v1/posts',
				array(
					'page'     => '1',
					'per_page' => '10',
				)
			)
		);

		$this->assertSame( 200, $response->get_status() );
	}

	public function test_rest_route_mode_is_accepted(): void {
		$credentials = $this->pair();
		$request     = $this->signed_request( $credentials, 'GET', '/faro/v1/pages', array( 'page' => '1' ) );
		$request->set_query_params(
			array(
				'rest_route' => '/faro/v1/pages',
				'page'       => '1',
			)
		);

		$this->assertSame( 200, $this->dispatch( $request )->get_status() );
	}

	/**
	 * Cada campo de la canónica alterado invalida la firma.
	 *
	 * @return array<string, array{0: array<string, string|int>}>
	 */
	public function altered_fields(): array {
		return array(
			'método'   => array( array( 'sign_method' => 'POST' ) ),
			'ruta'     => array( array( 'sign_route' => '/faro/v1/pages?page=1' ) ),
			'consulta' => array( array( 'sign_route' => '/faro/v1/posts?page=2' ) ),
			'hora'     => array( array( 'sign_timestamp' => self::NOW + 1 ) ),
			'nonce'    => array( array( 'sign_nonce' => 'AAAAAAAAAAAAAAAAAAAAAA' ) ),
			'cuerpo'   => array( array( 'sign_body' => '{"x":1}' ) ),
			'firma'    => array( array( 'signature' => base64_encode( str_repeat( 'x', 32 ) ) ) ), // phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_encode -- Firma de prueba.
		);
	}

	/**
	 * Firma alterada.
	 *
	 * @dataProvider altered_fields
	 *
	 * @param array<string, string|int> $overrides Alteración.
	 * @return void
	 */
	public function test_altered_signature_is_rejected( array $overrides ): void {
		$credentials = $this->pair();

		$response = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/posts', array( 'page' => '1' ), '', $overrides ) );

		$this->assertSame( 401, $response->get_status() );
		$this->assertSame( 'wp.invalid_signature', $this->error_code( $response ) );
	}

	public function test_request_outside_window_is_stale(): void {
		$credentials = $this->pair();

		foreach ( array( self::NOW - 301, self::NOW + 301 ) as $timestamp ) {
			$response = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/status', array(), '', array( 'timestamp' => $timestamp ) ) );
			$this->assertSame( 401, $response->get_status() );
			$this->assertSame( 'wp.stale_request', $this->error_code( $response ) );
		}
		foreach ( array( self::NOW - 300, self::NOW + 300 ) as $timestamp ) {
			$response = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/status', array(), '', array( 'timestamp' => $timestamp ) ) );
			$this->assertSame( 200, $response->get_status() );
		}
	}

	public function test_repeated_nonce_is_rejected(): void {
		$credentials = $this->pair();
		$overrides   = array( 'nonce' => 'dGVzdC1ub25jZS0wMDAwMQ' );

		$this->assertSame( 200, $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/status', array(), '', $overrides ) )->get_status() );
		$again = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/status', array(), '', $overrides ) );

		$this->assertSame( 401, $again->get_status() );
		$this->assertSame( 'wp.invalid_signature', $this->error_code( $again ) );
	}

	public function test_nonce_is_stored_only_after_valid_signature(): void {
		$credentials = $this->pair();
		$nonce       = 'dGVzdC1ub25jZS0wMDAwMg';
		$key         = Faro_Signature::NONCE_TRANSIENT_PREFIX . hash( 'sha256', $nonce );

		$this->dispatch(
			$this->signed_request(
				$credentials,
				'GET',
				'/faro/v1/status',
				array(),
				'',
				array(
					'nonce'     => $nonce,
					'signature' => base64_encode( str_repeat( 'y', 32 ) ), // phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_encode -- Firma de prueba.
				)
			)
		); // phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_encode -- Firma de prueba.
		$this->assertFalse( get_transient( $key ) );

		$this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/status', array(), '', array( 'nonce' => $nonce ) ) );
		$this->assertNotFalse( get_transient( $key ) );
	}

	/**
	 * Cabeceras obligatorias.
	 *
	 * @return array<string, array{0: string}>
	 */
	public function header_names(): array {
		return array(
			'conexión' => array( 'X-Faro-Connection' ),
			'token'    => array( 'X-Faro-Token' ),
			'hora'     => array( 'X-Faro-Timestamp' ),
			'nonce'    => array( 'X-Faro-Nonce' ),
			'firma'    => array( 'X-Faro-Signature' ),
		);
	}

	/**
	 * Falta una cabecera.
	 *
	 * @dataProvider header_names
	 *
	 * @param string $header Cabecera que falta.
	 * @return void
	 */
	public function test_missing_header_is_rejected( string $header ): void {
		$credentials = $this->pair();

		$response = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/status', array(), '', array( 'omit_' . $header => 1 ) ) );

		$this->assertSame( 401, $response->get_status() );
		$this->assertSame( 'wp.invalid_signature', $this->error_code( $response ) );
	}

	public function test_wrong_token_is_rejected(): void {
		$credentials = $this->pair();

		$response = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/status', array(), '', array( 'token' => str_repeat( 'A', 43 ) ) ) );

		$this->assertSame( 401, $response->get_status() );
		$this->assertSame( 'wp.invalid_signature', $this->error_code( $response ) );
	}

	public function test_unknown_connection_or_none_is_revoked(): void {
		$credentials = $this->pair();

		$other = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/status', array(), '', array( 'connection_id' => wp_generate_uuid4() ) ) );
		$this->assertSame( 'wp.revoked', $this->error_code( $other ) );

		Faro_Connection::revoke();
		$none = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/status' ) );
		$this->assertSame( 401, $none->get_status() );
		$this->assertSame( 'wp.revoked', $this->error_code( $none ) );
	}

	public function test_changed_salts_break_connection(): void {
		$credentials = $this->pair();
		add_filter(
			'salt',
			static function ( $salt, $scheme ) {
				return 'auth' === $scheme ? 'salts-nuevas-de-prueba' : $salt;
			},
			10,
			2
		);

		$response = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/status' ) );

		$this->assertSame( 401, $response->get_status() );
		$this->assertSame( 'wp.connection_broken', $this->error_code( $response ) );
		$this->assertTrue( Faro_Connection::is_broken() );
	}

	public function test_last_seen_is_updated_at_most_every_five_minutes(): void {
		$credentials = $this->pair();

		Faro_Clock::freeze( self::NOW + 299 );
		$this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/status' ) );
		$this->assertSame( self::NOW, Faro_Connection::get()['last_seen_at'] ?? null );

		Faro_Clock::freeze( self::NOW + 300 );
		$this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/status' ) );
		$this->assertSame( self::NOW + 300, Faro_Connection::get()['last_seen_at'] ?? null );
	}

	public function test_array_query_values_are_rejected(): void {
		$this->assertNull( Faro_Signature::canonical_route( '/faro/v1/posts', array( 'page' => array( '1' ) ) ) );
	}
}
