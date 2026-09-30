<?php
/**
 * Detección del plugin SEO activo.
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

/**
 * Detecta Yoast SEO o Rank Math. Los adaptadores de escritura (Faro_Seo_*) llegan en F4.
 */
final class Faro_Seo_Detector {

	/**
	 * Plugin SEO detectado.
	 *
	 * @return string "yoast", "rank_math" o "none".
	 */
	public static function detect(): string {
		if ( defined( 'WPSEO_VERSION' ) ) {
			return 'yoast';
		}
		if ( defined( 'RANK_MATH_VERSION' ) ) {
			return 'rank_math';
		}

		return 'none';
	}
}
