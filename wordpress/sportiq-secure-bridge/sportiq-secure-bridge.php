<?php
/**
 * Plugin Name: SportIQ Secure Bridge
 * Description: Server-side HMAC client for SportIQ FastAPI.
 * Version: 0.2.0
 * Requires PHP: 7.4
 */
if (!defined('ABSPATH')) exit;

if (!class_exists('SportIQ_Secure_Bridge')) {
class SportIQ_Secure_Bridge {
    private $base_url = 'https://sportiq-api-9fdb.onrender.com';
    private $path = '/v1/internal/hmac-check';

    public function send_secure_request($fixture_data) {
        if (!defined('SPORTIQ_WP_API_KEY') || !defined('SPORTIQ_WP_HMAC_SECRET')
            || !is_string(SPORTIQ_WP_API_KEY) || SPORTIQ_WP_API_KEY === ''
            || !is_string(SPORTIQ_WP_HMAC_SECRET) || SPORTIQ_WP_HMAC_SECRET === '') {
            return new WP_Error('sportiq_config', 'SportIQ 서버 인증 설정이 없습니다.');
        }
        if (!is_array($fixture_data) || !isset($fixture_data['fixture_id'])
            || filter_var($fixture_data['fixture_id'], FILTER_VALIDATE_INT, array('options' => array('min_range' => 1))) === false) {
            return new WP_Error('sportiq_input', '올바른 fixture_id가 필요합니다.');
        }
        $body = wp_json_encode(array('fixture_id' => (int) $fixture_data['fixture_id']));
        if (!is_string($body)) {
            return new WP_Error('sportiq_json', '요청을 인코딩하지 못했습니다.');
        }
        $timestamp = (string) time();
        $message = "POST\n" . $this->path . "\n" . $timestamp . "\n" . $body;
        $signature = hash_hmac('sha256', $message, SPORTIQ_WP_HMAC_SECRET);
        $response = wp_remote_post($this->base_url . $this->path, array(
            'timeout' => 15,
            'redirection' => 0,
            'headers' => array(
                'Content-Type' => 'application/json',
                'X-API-Key' => SPORTIQ_WP_API_KEY,
                'X-Timestamp' => $timestamp,
                'X-Signature' => $signature,
            ),
            'body' => $body,
            'data_format' => 'body',
        ));
        if (is_wp_error($response)) {
            return new WP_Error('sportiq_connection', 'SportIQ API에 연결하지 못했습니다.');
        }
        if (wp_remote_retrieve_response_code($response) !== 200) {
            return new WP_Error('sportiq_api', 'SportIQ 인증 요청이 실패했습니다.');
        }
        $decoded = json_decode(wp_remote_retrieve_body($response), true);
        if (!is_array($decoded) || !isset($decoded['authenticated']) || $decoded['authenticated'] !== true) {
            return new WP_Error('sportiq_response', 'SportIQ 인증 응답을 확인하지 못했습니다.');
        }
        return $decoded;
    }
}
}

// Public prediction reads are separate from the authenticated HMAC bridge.
require_once __DIR__ . '/class-sportiq-api-client.php';

require_once __DIR__ . '/sportiq-predictor.php';
