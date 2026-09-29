<?php
/**
 * Lectura de páginas, entradas y productos publicados.
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

/**
 * Devuelve páginas de contenido publicado con títulos en texto plano y fechas UTC.
 */
final class Faro_Content {

	/**
	 * Tipos de contenido que se pueden leer, por ruta.
	 */
	public const KINDS = array( 'pages', 'posts', 'products' );

	/**
	 * Lista una página de contenido.
	 *
	 * @param string $kind     "pages", "posts" o "products".
	 * @param int    $page     Página (desde 1).
	 * @param int    $per_page Elementos por página (1–100).
	 * @return array{items: list<array{id: int, title: string, url: string, slug: string, modified_at: string}>, page: int, per_page: int, total: int, total_pages: int, woocommerce_active: bool}
	 */
	public static function get_page( string $kind, int $page, int $per_page ): array {
		$result = 'products' === $kind
			? Faro_WooCommerce::list_products( $page, $per_page )
			: self::list_posts( 'pages' === $kind ? 'page' : 'post', $page, $per_page );

		return array(
			'items'              => $result['items'],
			'page'               => $page,
			'per_page'           => $per_page,
			'total'              => $result['total'],
			'total_pages'        => (int) ceil( $result['total'] / $per_page ),
			'woocommerce_active' => Faro_WooCommerce::is_active(),
		);
	}

	/**
	 * Convierte un título a texto plano (sin etiquetas ni entidades HTML).
	 *
	 * @param string $text Título.
	 * @return string
	 */
	public static function plain_text( string $text ): string {
		return html_entity_decode( wp_strip_all_tags( $text ), ENT_QUOTES, 'UTF-8' );
	}

	/**
	 * Lista entradas o páginas publicadas.
	 *
	 * @param string $post_type "page" o "post".
	 * @param int    $page      Página.
	 * @param int    $per_page  Elementos por página.
	 * @return array{items: list<array{id: int, title: string, url: string, slug: string, modified_at: string}>, total: int}
	 */
	private static function list_posts( string $post_type, int $page, int $per_page ): array {
		$query = new WP_Query(
			array(
				'post_type'           => $post_type,
				'post_status'         => 'publish',
				'orderby'             => 'ID',
				'order'               => 'ASC',
				'posts_per_page'      => $per_page,
				'paged'               => $page,
				'ignore_sticky_posts' => true,
			)
		);
		$items = array();
		foreach ( is_array( $query->posts ) ? $query->posts : array() as $post ) {
			if ( ! $post instanceof WP_Post ) {
				continue;
			}
			$modified = get_post_modified_time( 'U', true, $post );
			$items[]  = array(
				'id'          => $post->ID,
				'title'       => self::plain_text( get_the_title( $post ) ),
				'url'         => (string) get_permalink( $post ),
				'slug'        => $post->post_name,
				'modified_at' => Faro_Clock::iso( is_numeric( $modified ) ? (int) $modified : 0 ),
			);
		}

		return array(
			'items' => $items,
			'total' => (int) $query->found_posts,
		);
	}
}
