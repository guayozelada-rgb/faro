<?php
/**
 * Pruebas de la vinculación con código.
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

/**
 * POST /faro/v1/pair y Faro_Pairing.
 */
class Test_Faro_Pairing extends Faro_Test_Case {

	/**
	 * Autoload de una opción según la base.
	 *
	 * @param string $name Opción.
	 * @return string
	 */
	private function autoload_of( string $name ): string {
		global $wpdb;
		return (string) $wpdb->get_var( $wpdb->prepare( "SELECT autoload FROM {$wpdb->options} WHERE option_name = %s", $name ) ); // phpcs:ignore WordPress.DB.DirectDatabaseQuery
	}

	public function test_create_code_has_six_digits_and_only_its_hash_is_stored(): void {
		$code = Faro_Pairing::create_code();

		$this->assertMatchesRegularExpression( '/^[0-9]{6}$/', $code );
		$stored = get_option( Faro_Pairing::OPTION );
		$this->assertIsArray( $stored );
		$this->assertSame( Faro_Pairing::hash_code( $code ), $stored['code_hash'] );
		$this->assertSame( self::NOW + 600, $stored['expires_at'] );
		$this->assertSame( 5, $stored['attempts_left'] );
		$this->assertStringNotContainsString( $code, (string) wp_json_encode( $stored ) );
		$this->assertContains( $this->autoload_of( Faro_Pairing::OPTION ), array( 'no', 'off' ) );
	}

	public function test_pair_success_returns_credentials_once_and_stores_only_hash_and_ciphertext(): void {
		$code     = Faro_Pairing::create_code();
		$response = $this->pair_request( $this->pair_body( $code ) );

		$this->assertSame( 200, $response->get_status() );
		$data = $response->get_data();
		$this->assertSame( 1, $data['api_version'] );
		$this->assertTrue( wp_is_uuid( $data['connection_id'], 4 ) );
		$this->assertMatchesRegularExpression( '/^[A-Za-z0-9_-]{43}$/', $data['token'] );
		$this->assertMatchesRegularExpression( '/^[A-Za-z0-9_-]{43}$/', $data['hmac_secret'] );
		$this->assertSame( FARO_VERSION, $data['site']['plugin_version'] );
		$this->assertSame( untrailingslashit( home_url() ), $data['site']['home_url'] );
		$this->assertArrayHasKey( 'name', $data['site'] );
		$this->assertArrayHasKey( 'wp_version', $data['site'] );

		$this->assertFalse( get_option( Faro_Pairing::OPTION ), 'El código se borra al usarlo.' );

		$stored = get_option( Faro_Connection::OPTION );
		$this->assertIsArray( $stored );
		$this->assertSame( $data['connection_id'], $stored['connection_id'] );
		$this->assertSame( hash( 'sha256', $data['token'] ), $stored['token_sha256'] );
		$this->assertStringStartsWith( 'v1:', $stored['hmac_secret'] );
		$this->assertSame( '0192a6f0-7c1e-7b3a-9f00-000000000001', $stored['app_instance_id'] );
		$this->assertSame( '0.1.0', $stored['app_version'] );
		$this->assertSame( self::NOW, $stored['created_at'] );
		$this->assertSame( self::NOW, $stored['last_seen_at'] );
		$serialized = (string) wp_json_encode( $stored );
		$this->assertStringNotContainsString( $data['token'], $serialized );
		$this->assertStringNotContainsString( $data['hmac_secret'], $serialized );
		$this->assertContains( $this->autoload_of( Faro_Connection::OPTION ), array( 'no', 'off' ) );
	}

	public function test_reused_code_is_rejected(): void {
		$code = Faro_Pairing::create_code();
		$this->assertSame( 200, $this->pair_request( $this->pair_body( $code ) )->get_status() );

		$again = $this->pair_request( $this->pair_body( $code ) );

		$this->assertSame( 410, $again->get_status() );
		$this->assertSame( 'wp.pairing_expired', $this->error_code( $again ) );
	}

	public function test_expired_code_is_rejected_and_deleted(): void {
		$code = Faro_Pairing::create_code();
		Faro_Clock::freeze( self::NOW + 600 );

		$response = $this->pair_request( $this->pair_body( $code ) );

		$this->assertSame( 410, $response->get_status() );
		$this->assertSame( 'wp.pairing_expired', $this->error_code( $response ) );
		$this->assertFalse( get_option( Faro_Pairing::OPTION ) );
	}

	public function test_code_still_valid_just_before_expiry(): void {
		$code = Faro_Pairing::create_code();
		Faro_Clock::freeze( self::NOW + 599 );

		$this->assertSame( 200, $this->pair_request( $this->pair_body( $code ) )->get_status() );
	}

	public function test_wrong_code_consumes_attempts_until_exhausted(): void {
		$code  = Faro_Pairing::create_code();
		$wrong = '000000' === $code ? '111111' : '000000';

		foreach ( array( 4, 3, 2, 1, 0 ) as $expected_left ) {
			$response = $this->pair_request( $this->pair_body( $wrong ) );
			$this->assertSame( 403, $response->get_status() );
			$this->assertSame( 'wp.pairing_invalid', $this->error_code( $response ) );
			$this->assertSame( $expected_left, $response->get_data()['data']['attempts_left'] );
		}

		$this->assertFalse( get_option( Faro_Pairing::OPTION ), 'A 0 intentos el código se borra.' );
		$correct = $this->pair_request( $this->pair_body( $code ) );
		$this->assertSame( 410, $correct->get_status() );
		$this->assertSame( 'wp.pairing_expired', $this->error_code( $correct ) );
	}

	public function test_ip_limit_blocks_after_ten_failures_and_resets_after_window(): void {
		for ( $i = 0; $i < 10; $i++ ) {
			if ( 0 === $i % 5 ) {
				$code  = Faro_Pairing::create_code();
				$wrong = '000000' === $code ? '111111' : '000000';
			}
			$this->assertSame( 403, $this->pair_request( $this->pair_body( $wrong ) )->get_status() );
		}

		$code     = Faro_Pairing::create_code();
		$response = $this->pair_request( $this->pair_body( $code ) );
		$this->assertSame( 429, $response->get_status() );
		$this->assertSame( 'wp.rate_limited', $this->error_code( $response ) );
		$this->assertIsArray( get_option( Faro_Pairing::OPTION ), 'El límite por IP no consume el código.' );

		$_SERVER['REMOTE_ADDR']          = '198.51.100.7';
		$_SERVER['HTTP_X_FORWARDED_FOR'] = '203.0.113.10';
		$this->assertSame( 200, $this->pair_request( $this->pair_body( $code ) )->get_status(), 'Otra IP no está bloqueada; X-Forwarded-For se ignora.' );
		unset( $_SERVER['HTTP_X_FORWARDED_FOR'] );

		$_SERVER['REMOTE_ADDR'] = '203.0.113.10';
		Faro_Clock::freeze( self::NOW + 900 );
		$code = Faro_Pairing::create_code();
		$this->assertSame( 200, $this->pair_request( $this->pair_body( $code ) )->get_status(), 'La ventana de 15 minutos terminó.' );
	}

	public function test_code_with_leading_zeros_works(): void {
		update_option(
			Faro_Pairing::OPTION,
			array(
				'code_hash'     => Faro_Pairing::hash_code( '004821' ),
				'expires_at'    => self::NOW + 600,
				'attempts_left' => 5,
			),
			false
		);

		$short = $this->pair_request( $this->pair_body( '4821' ) );
		$this->assertSame( 400, $short->get_status() );
		$this->assertSame( 'wp.invalid_input', $this->error_code( $short ) );

		$this->assertSame( 200, $this->pair_request( $this->pair_body( '004821' ) )->get_status() );
	}

	public function test_insecure_site_outside_local_cannot_pair(): void {
		$this->force_scheme( 'http' );
		$code = Faro_Pairing::create_code();
		Faro_Pairing::set_environment_type( 'production' );

		$response = $this->pair_request( $this->pair_body( $code ) );

		$this->assertSame( 403, $response->get_status() );
		$this->assertSame( 'wp.insecure_site', $this->error_code( $response ) );
	}

	public function test_https_site_outside_local_can_pair(): void {
		Faro_Pairing::set_environment_type( 'production' );
		$this->force_scheme( 'https' );
		$code = Faro_Pairing::create_code();

		$this->assertSame( 200, $this->pair_request( $this->pair_body( $code ) )->get_status() );
	}

	public function test_invalid_input_is_rejected_after_authorization(): void {
		$no_code = $this->pair_request( array( 'code' => 'abc' ) );
		$this->assertSame( 410, $no_code->get_status(), 'Sin código pendiente, primero se informa eso.' );

		Faro_Pairing::create_code();
		$cases = array(
			array( 'code' => '123456' ),
			array_merge( $this->pair_body( '123456' ), array( 'app_instance_id' => 'no-es-uuid' ) ),
			array_merge( $this->pair_body( '123456' ), array( 'app_version' => str_repeat( 'x', 40 ) ) ),
			array_merge( $this->pair_body( '12345a' ) ),
		);
		foreach ( $cases as $body ) {
			$response = $this->pair_request( $body );
			$this->assertSame( 400, $response->get_status() );
			$this->assertSame( 'wp.invalid_input', $this->error_code( $response ) );
		}
		$this->assertSame( 5, get_option( Faro_Pairing::OPTION )['attempts_left'], 'Una entrada inválida no consume intentos.' );
	}

	public function test_new_code_invalidates_previous(): void {
		$first  = Faro_Pairing::create_code();
		$second = Faro_Pairing::create_code();
		if ( $first === $second ) {
			$this->markTestSkipped( 'Coincidencia aleatoria de códigos.' );
		}

		$this->assertSame( 403, $this->pair_request( $this->pair_body( $first ) )->get_status() );
		$this->assertSame( 200, $this->pair_request( $this->pair_body( $second ) )->get_status() );
	}

	public function test_pairing_again_replaces_previous_connection(): void {
		$old = $this->pair();
		$new = $this->pair();

		$old_response = $this->dispatch( $this->signed_request( $old, 'GET', '/faro/v1/status' ) );
		$this->assertSame( 401, $old_response->get_status() );
		$this->assertSame( 'wp.revoked', $this->error_code( $old_response ) );

		$this->assertSame( 200, $this->dispatch( $this->signed_request( $new, 'GET', '/faro/v1/status' ) )->get_status() );
	}

	public function test_pair_response_is_not_cacheable(): void {
		$response = $this->pair_request( $this->pair_body( Faro_Pairing::create_code() ) );

		$this->assertSame( 'no-store, private', $response->get_headers()['Cache-Control'] ?? null );
	}
}
