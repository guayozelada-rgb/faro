<?php
/**
 * Conexión activa con la app Faro.
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

/**
 * Guarda una sola conexión activa en la opción faro_connection (autoload = no).
 * Solo guarda sha256( token ) y el secreto HMAC cifrado (ADR 0011 §4–5).
 *
 * @phpstan-type FaroConnection array{connection_id: string, token_sha256: string, hmac_secret: string, app_instance_id: string, app_version: string, created_at: int, last_seen_at: int}
 */
final class Faro_Connection {

	/**
	 * Nombre de la opción.
	 */
	public const OPTION = 'faro_connection';

	/**
	 * Cada cuánto se actualiza last_seen_at como mucho (segundos).
	 */
	private const LAST_SEEN_INTERVAL = 300;

	/**
	 * Crea una conexión nueva y reemplaza la anterior.
	 *
	 * @param string $app_instance_id UUID de la conexión en la app.
	 * @param string $app_version     Versión de la app.
	 * @return array{connection_id: string, token: string, hmac_secret: string} Credenciales en claro (se entregan una sola vez).
	 */
	public static function create( string $app_instance_id, string $app_version ): array {
		$token  = Faro_Crypto::base64url_encode( Faro_Crypto::random_key() );
		$secret = Faro_Crypto::random_key();
		$now    = Faro_Clock::now();
		$record = array(
			'connection_id'   => wp_generate_uuid4(),
			'token_sha256'    => hash( 'sha256', $token ),
			'hmac_secret'     => Faro_Crypto::encrypt( $secret ),
			'app_instance_id' => $app_instance_id,
			'app_version'     => $app_version,
			'created_at'      => $now,
			'last_seen_at'    => $now,
		);

		delete_option( self::OPTION );
		add_option( self::OPTION, $record, '', false );

		return array(
			'connection_id' => $record['connection_id'],
			'token'         => $token,
			'hmac_secret'   => Faro_Crypto::base64url_encode( $secret ),
		);
	}

	/**
	 * Conexión guardada, o null si no hay (o si la opción está dañada).
	 *
	 * @return array<string, mixed>|null
	 * @phpstan-return FaroConnection|null
	 */
	public static function get(): ?array {
		return self::validate( get_option( self::OPTION ) );
	}

	/**
	 * Valida la forma de una conexión guardada.
	 *
	 * @param mixed $record Valor de la opción.
	 * @return array<string, mixed>|null
	 * @phpstan-return FaroConnection|null
	 */
	private static function validate( $record ): ?array {
		if ( ! is_array( $record ) ) {
			return null;
		}
		foreach ( array( 'connection_id', 'token_sha256', 'hmac_secret', 'app_instance_id', 'app_version' ) as $key ) {
			if ( ! isset( $record[ $key ] ) || ! is_string( $record[ $key ] ) ) {
				return null;
			}
		}
		foreach ( array( 'created_at', 'last_seen_at' ) as $key ) {
			if ( ! isset( $record[ $key ] ) || ! is_int( $record[ $key ] ) ) {
				return null;
			}
		}

		/**
		 * Conexión validada.
		 *
		 * @phpstan-var FaroConnection $record
		 */
		return $record;
	}

	/**
	 * Secreto HMAC (32 bytes) de una conexión, o null si no se puede descifrar.
	 *
	 * @param array<string, mixed> $connection Conexión guardada.
	 * @phpstan-param FaroConnection $connection
	 * @return string|null
	 */
	public static function secret( array $connection ): ?string {
		$secret = Faro_Crypto::decrypt( $connection['hmac_secret'] );

		return ( null !== $secret && 32 === strlen( $secret ) ) ? $secret : null;
	}

	/**
	 * Indica si hay conexión guardada pero su secreto ya no se puede descifrar (salts cambiadas).
	 *
	 * @return bool
	 */
	public static function is_broken(): bool {
		$connection = self::get();

		return null !== $connection && null === self::secret( $connection );
	}

	/**
	 * Actualiza last_seen_at como mucho cada 5 minutos.
	 *
	 * Escribe con comparar e intercambiar sobre el valor exacto leído de la base y nunca recrea
	 * la opción: si mientras tanto se desconectó el sitio (opción borrada) o se vinculó otra
	 * conexión (valor distinto), no hace nada. Así una petición en curso no revive una conexión
	 * revocada ni pisa una vinculación nueva.
	 *
	 * @param array<string, mixed> $connection Conexión verificada en esta petición.
	 * @phpstan-param FaroConnection $connection
	 * @return void
	 */
	public static function touch( array $connection ): void {
		$now = Faro_Clock::now();
		if ( $now - $connection['last_seen_at'] < self::LAST_SEEN_INTERVAL ) {
			return;
		}

		$raw = Faro_Option_Store::read_raw( self::OPTION );
		if ( null === $raw ) {
			return;
		}
		$stored = self::validate( maybe_unserialize( $raw ) );
		if ( null === $stored
			|| ! hash_equals( $stored['connection_id'], $connection['connection_id'] )
			|| ! hash_equals( $stored['token_sha256'], $connection['token_sha256'] )
			|| $now - $stored['last_seen_at'] < self::LAST_SEEN_INTERVAL ) {
			return;
		}

		$stored['last_seen_at'] = $now;
		Faro_Option_Store::compare_and_swap( self::OPTION, $raw, maybe_serialize( $stored ) );
	}

	/**
	 * Borra la conexión (revocación).
	 *
	 * @return void
	 */
	public static function revoke(): void {
		delete_option( self::OPTION );
	}
}
