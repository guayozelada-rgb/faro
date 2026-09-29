<?php
/**
 * Reloj del plugin (inyectable en pruebas).
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

/**
 * Da la hora Unix actual. Las pruebas pueden fijarla para ser deterministas.
 */
final class Faro_Clock {

	/**
	 * Hora fijada por las pruebas, o null para usar la hora real.
	 *
	 * @var int|null
	 */
	private static ?int $frozen = null;

	/**
	 * Hora Unix actual en segundos.
	 *
	 * @return int
	 */
	public static function now(): int {
		return self::$frozen ?? time();
	}

	/**
	 * Fija la hora (solo pruebas). Con null vuelve a la hora real.
	 *
	 * @param int|null $timestamp Hora Unix o null.
	 * @return void
	 */
	public static function freeze( ?int $timestamp ): void {
		self::$frozen = $timestamp;
	}

	/**
	 * Formatea una hora Unix como ISO-8601 en UTC con sufijo Z.
	 *
	 * @param int $timestamp Hora Unix.
	 * @return string
	 */
	public static function iso( int $timestamp ): string {
		return gmdate( 'Y-m-d\TH:i:s\Z', $timestamp );
	}
}
