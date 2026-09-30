<?php
/**
 * Pruebas de lectura de páginas y entradas.
 *
 * @package Faro
 */

defined( 'ABSPATH' ) || exit;

/**
 * GET /faro/v1/pages y /faro/v1/posts.
 */
class Test_Faro_Content extends Faro_Test_Case {

	public function test_posts_are_paginated_published_only_and_ordered_by_id(): void {
		$credentials = $this->pair();
		$published   = self::factory()->post->create_many( 3, array( 'post_status' => 'publish' ) );
		self::factory()->post->create( array( 'post_status' => 'draft' ) );
		self::factory()->post->create( array( 'post_status' => 'private' ) );
		self::factory()->post->create(
			array(
				'post_status' => 'future',
				'post_date'   => '2099-01-01 00:00:00',
			)
		);
		$all_published = get_posts(
			array(
				'post_type'   => 'post',
				'post_status' => 'publish',
				'numberposts' => -1,
				'orderby'     => 'ID',
				'order'       => 'ASC',
				'fields'      => 'ids',
			)
		);
		$this->assertCount( count( $all_published ), array_unique( array_merge( $all_published, $published ) ) );

		$first  = $this->dispatch(
			$this->signed_request(
				$credentials,
				'GET',
				'/faro/v1/posts',
				array(
					'page'     => '1',
					'per_page' => '2',
				)
			)
		)->get_data();
		$second = $this->dispatch(
			$this->signed_request(
				$credentials,
				'GET',
				'/faro/v1/posts',
				array(
					'page'     => '2',
					'per_page' => '2',
				)
			)
		)->get_data();

		$total = count( $all_published );
		$this->assertSame( $total, $first['total'] );
		$this->assertSame( (int) ceil( $total / 2 ), $first['total_pages'] );
		$this->assertSame( 1, $first['page'] );
		$this->assertSame( 2, $first['per_page'] );
		$ids = array_merge( wp_list_pluck( $first['items'], 'id' ), wp_list_pluck( $second['items'], 'id' ) );
		$this->assertSame( array_slice( array_map( 'intval', $all_published ), 0, 4 ), $ids );
		$this->assertIsBool( $first['woocommerce_active'] );
	}

	public function test_default_pagination(): void {
		$credentials = $this->pair();

		$data = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/pages' ) )->get_data();

		$this->assertSame( 1, $data['page'] );
		$this->assertSame( 50, $data['per_page'] );
	}

	public function test_page_beyond_total_is_empty(): void {
		$credentials = $this->pair();

		$data = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/pages', array( 'page' => '999' ) ) )->get_data();

		$this->assertSame( array(), $data['items'] );
	}

	public function test_items_have_plain_titles_urls_slugs_and_utc_dates(): void {
		$credentials = $this->pair();
		$page_id     = self::factory()->post->create(
			array(
				'post_type'         => 'page',
				'post_status'       => 'publish',
				'post_title'        => 'Café <strong>&amp;</strong> té &#8220;especial&#8221;',
				'post_name'         => 'cafe-y-te',
				'post_date'         => '2026-01-02 03:04:05',
				'post_date_gmt'     => '2026-01-02 09:04:05',
				'post_modified'     => '2026-01-02 03:04:05',
				'post_modified_gmt' => '2026-01-02 09:04:05',
			)
		);
		global $wpdb;
		$wpdb->update( $wpdb->posts, array( 'post_modified_gmt' => '2026-02-03 10:11:12' ), array( 'ID' => $page_id ) ); // phpcs:ignore WordPress.DB.DirectDatabaseQuery
		clean_post_cache( $page_id );

		$data  = $this->dispatch( $this->signed_request( $credentials, 'GET', '/faro/v1/pages', array( 'per_page' => '100' ) ) )->get_data();
		$items = array_values( array_filter( $data['items'], static fn( $item ) => $page_id === $item['id'] ) );

		$this->assertCount( 1, $items );
		$this->assertSame( array( 'id', 'title', 'url', 'slug', 'modified_at' ), array_keys( $items[0] ) );
		$this->assertSame( "Café & té \u{201C}especial\u{201D}", $items[0]['title'] );
		$this->assertSame( get_permalink( $page_id ), $items[0]['url'] );
		$this->assertSame( 'cafe-y-te', $items[0]['slug'] );
		$this->assertSame( '2026-02-03T10:11:12Z', $items[0]['modified_at'] );
	}
}
