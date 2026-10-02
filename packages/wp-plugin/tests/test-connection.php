<?php
/**
 * Pruebas de last_seen_at: touch() no revive conexiones revocadas ni pisa vinculaciones nuevas.
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

/**
 * Faro_Connection::touch() y Faro_Option_Store.
 */
class Test_Faro_Connection extends Faro_Test_Case {

	/**
	 * Crea una conexión directamente (sin pasar por /pair) y la devuelve validada.
	 *
	 * @return array<string, mixed>
	 */
	private function create_connection(): array {
		Faro_Connection::create( wp_generate_uuid4(), '0.1.0' );
		$connection = Faro_Connection::get();
		$this->assertIsArray( $connection );

		return $connection;
	}

	/**
	 * Ejecuta una acción justo antes del primer UPDATE de faro_connection (simula otra petición
	 * simultánea entre la lectura y la escritura de touch()).
	 *
	 * @param callable $action Acción.
	 * @return void
	 */
	private function inject_before_connection_update( callable $action ): void {
		$done = false;
		add_filter(
			'query',
			static function ( $query ) use ( &$done, $action ) {
				if ( ! $done && is_string( $query ) && str_starts_with( ltrim( $query ), 'UPDATE' ) && str_contains( $query, "'faro_connection'" ) ) {
					$done = true;
					$action();
				}
				return $query;
			}
		);
	}

	/**
	 * Autoload guardado de una opción.
	 *
	 * @param string $option Opción.
	 * @return string
	 */
	private function autoload_of( string $option ): string {
		global $wpdb;
		// phpcs:ignore WordPress.DB.DirectDatabaseQuery.DirectQuery, WordPress.DB.DirectDatabaseQuery.NoCaching -- Prueba.
		return (string) $wpdb->get_var( $wpdb->prepare( "SELECT autoload FROM {$wpdb->options} WHERE option_name = %s", $option ) );
	}

	// (a) touch() con la opción borrada no la recrea.

	public function test_touch_after_revoke_does_not_recreate_option(): void {
		$connection = $this->create_connection();
		Faro_Connection::revoke();

		Faro_Clock::freeze( self::NOW + 300 );
		Faro_Connection::touch( $connection );

		$this->assertNull( Faro_Option_Store::read_raw( Faro_Connection::OPTION ), 'La conexión revocada no reaparece en la base.' );
		$this->assertFalse( get_option( Faro_Connection::OPTION ) );
		$this->assertNull( Faro_Connection::get() );
	}

	public function test_revoke_between_read_and_write_does_not_recreate_option(): void {
		$connection = $this->create_connection();
		$this->inject_before_connection_update(
			static function (): void {
				Faro_Connection::revoke(); // El administrador pulsa "Desconectar".
			}
		);

		Faro_Clock::freeze( self::NOW + 300 );
		Faro_Connection::touch( $connection );

		$this->assertNull( Faro_Option_Store::read_raw( Faro_Connection::OPTION ), 'La conexión revocada no reaparece en la base.' );
		$this->assertNull( Faro_Connection::get() );
	}

	public function test_disconnect_during_signed_request_stays_revoked(): void {
		$credentials = $this->pair();
		Faro_Clock::freeze( self::NOW + 300 );
		$this->inject_before_connection_update(
			static function (): void {
				Faro_Connection::revoke();
			}
		);

		$this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/status' ) );

		$this->assertNull( Faro_Option_Store::read_raw( Faro_Connection::OPTION ) );
		$this->assertNull( Faro_Connection::get() );
		$after = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/status' ) );
		$this->assertSame( 401, $after->get_status() );
		$this->assertSame( 'wp.revoked', $this->error_code( $after ) );
	}

	// (b) touch() con una conexión distinta de la leída no pisa la nueva.

	public function test_touch_with_old_connection_does_not_overwrite_new_one(): void {
		$old = $this->create_connection();
		Faro_Clock::freeze( self::NOW + 100 );
		$new     = $this->create_connection();
		$new_raw = Faro_Option_Store::read_raw( Faro_Connection::OPTION );

		Faro_Clock::freeze( self::NOW + 300 );
		Faro_Connection::touch( $old );

		$this->assertSame( $new_raw, Faro_Option_Store::read_raw( Faro_Connection::OPTION ), 'La conexión nueva queda intacta.' );
		$this->assertSame( $new['connection_id'], Faro_Connection::get()['connection_id'] ?? null );
	}

	public function test_repair_between_read_and_write_is_not_overwritten(): void {
		$old     = $this->create_connection();
		$new_raw = null;
		$this->inject_before_connection_update(
			static function () use ( &$new_raw ): void {
				// Otra computadora vincula el sitio en ese mismo instante.
				Faro_Connection::create( wp_generate_uuid4(), '0.1.0' );
				$new_raw = Faro_Option_Store::read_raw( Faro_Connection::OPTION );
			}
		);

		Faro_Clock::freeze( self::NOW + 300 );
		Faro_Connection::touch( $old );

		$this->assertIsString( $new_raw );
		$this->assertSame( $new_raw, Faro_Option_Store::read_raw( Faro_Connection::OPTION ), 'El registro viejo no pisa al nuevo.' );
		$stored = Faro_Connection::get();
		$this->assertIsArray( $stored );
		$this->assertNotSame( $old['connection_id'], $stored['connection_id'] );
	}

	public function test_repair_during_signed_request_keeps_new_connection(): void {
		$old_credentials = $this->pair();
		Faro_Clock::freeze( self::NOW + 300 );
		$new_credentials = null;
		$this->inject_before_connection_update(
			static function () use ( &$new_credentials ): void {
				$new_credentials = Faro_Connection::create( wp_generate_uuid4(), '0.1.0' );
			}
		);

		$this->dispatch( $this->signed_request( $old_credentials, 'GET', '/faro/v1/status' ) );
		remove_all_filters( 'query' );

		$this->assertIsArray( $new_credentials );
		$this->assertSame( $new_credentials['connection_id'], Faro_Connection::get()['connection_id'] ?? null );

		$old = $this->dispatch( $this->signed_request( $old_credentials, 'GET', '/faro/v1/status' ) );
		$this->assertSame( 401, $old->get_status() );
		$this->assertSame( 'wp.revoked', $this->error_code( $old ), 'La conexión vieja sigue revocada.' );

		$new = $this->dispatch( $this->signed_request( $new_credentials, 'GET', '/faro/v1/status' ) );
		$this->assertSame( 200, $new->get_status(), 'La conexión nueva sigue funcionando.' );
	}

	// (c) El caso normal sigue actualizando last_seen_at, como mucho cada 5 minutos.

	public function test_touch_updates_last_seen_at_most_every_five_minutes(): void {
		$this->create_connection();

		Faro_Clock::freeze( self::NOW + 299 );
		Faro_Connection::touch( (array) Faro_Connection::get() );
		$this->assertSame( self::NOW, Faro_Connection::get()['last_seen_at'] ?? null );

		Faro_Clock::freeze( self::NOW + 300 );
		Faro_Connection::touch( (array) Faro_Connection::get() );
		$stored = Faro_Connection::get();
		$this->assertSame( self::NOW + 300, $stored['last_seen_at'] ?? null, 'get() ve el valor nuevo: la caché se invalidó.' );
		$this->assertSame( self::NOW, $stored['created_at'] ?? null );
		$this->assertContains( $this->autoload_of( Faro_Connection::OPTION ), array( 'no', 'off' ), 'Sigue sin autoload.' );

		Faro_Clock::freeze( self::NOW + 599 );
		Faro_Connection::touch( (array) Faro_Connection::get() );
		$this->assertSame( self::NOW + 300, Faro_Connection::get()['last_seen_at'] ?? null );

		Faro_Clock::freeze( self::NOW + 600 );
		Faro_Connection::touch( (array) Faro_Connection::get() );
		$this->assertSame( self::NOW + 600, Faro_Connection::get()['last_seen_at'] ?? null );
	}

	public function test_touch_respects_interval_of_stored_value(): void {
		$stale = $this->create_connection();

		Faro_Clock::freeze( self::NOW + 300 );
		Faro_Connection::touch( $stale );
		$this->assertSame( self::NOW + 300, Faro_Connection::get()['last_seen_at'] ?? null );

		// Otra petición leyó antes de esa escritura: no vuelve a escribir dentro del intervalo.
		Faro_Clock::freeze( self::NOW + 400 );
		Faro_Connection::touch( $stale );
		$this->assertSame( self::NOW + 300, Faro_Connection::get()['last_seen_at'] ?? null );
	}

	public function test_option_store_compare_and_swap_never_creates_option(): void {
		$this->assertFalse( Faro_Option_Store::compare_and_swap( Faro_Connection::OPTION, 'a:0:{}', 'a:0:{}' ) );
		$this->assertNull( Faro_Option_Store::read_raw( Faro_Connection::OPTION ) );
	}
}
