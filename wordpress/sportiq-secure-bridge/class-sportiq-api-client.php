<?php
/**
 * SportIQ public prediction read client.
 * The ID is SportIQ matches.id, NOT an API-Football fixture ID.
 * This file does not expose a REST route or shortcode.
 */
if (!defined('ABSPATH')) { exit; }

if (!class_exists('SportIQ_API_Client')) {
class SportIQ_API_Client {
    const CACHE_TTL = 15 * MINUTE_IN_SECONDS;
    const STALE_CACHE_TTL = 24 * HOUR_IN_SECONDS;
    const REQUEST_TIMEOUT = 8;
    private $api_base_url;

    public function __construct() {
        $url = defined('SPORTIQ_API_URL') ? SPORTIQ_API_URL : 'https://sportiq-api-9fdb.onrender.com';
        $this->api_base_url = untrailingslashit($url);
    }

    public function get_prediction($match_id, $force_refresh = false) {
        $match_id = absint($match_id);
        if (!$match_id) {
            return $this->format_error('유효하지 않은 SportIQ 경기 ID입니다.');
        }
        // Do not permit anonymous callers to bypass the cache.
        if ($force_refresh && !current_user_can('manage_options')) {
            $force_refresh = false;
        }
        $fresh_key = 'sportiq_pred_' . $match_id;
        $stale_key = 'sportiq_pred_stale_' . $match_id;
        $cooldown_key = 'sportiq_pred_cool_' . $match_id;

        if (!$force_refresh) {
            $cached = get_transient($fresh_key);
            if (is_array($cached)) {
                return $this->with_meta($cached, 'cache', false);
            }
            if (get_transient($cooldown_key)) {
                $stale = get_transient($stale_key);
                if (is_array($stale)) {
                    return $this->with_meta($stale, 'stale_cache', true);
                }
            }
        }

        $result = $this->request_fastapi('/v1/predictions/' . $match_id);
        if (!is_wp_error($result)) {
            set_transient($fresh_key, $result, self::CACHE_TTL);
            set_transient($stale_key, $result, self::STALE_CACHE_TTL);
            delete_transient($cooldown_key);
            return $this->with_meta($result, 'api', false);
        }

        // A missing prediction (404) is not a temporary backend outage.
        if ($result->get_error_code() === 'sportiq_not_found') {
            return $this->format_error('해당 경기의 예측 데이터가 없습니다.');
        }

        $stale = get_transient($stale_key);
        if (is_array($stale)) {
            set_transient($cooldown_key, 1, 2 * MINUTE_IN_SECONDS);
            return $this->with_meta($stale, 'stale_cache', true);
        }
        return $this->format_error('현재 예측 서버 응답이 지연되고 있습니다. 잠시 후 다시 확인해 주세요.');
    }

    private function request_fastapi($path) {
        $base = wp_parse_url($this->api_base_url);
        $allowed_host = 'sportiq-api-9fdb.onrender.com';
        if (!is_array($base) || !isset($base['scheme'], $base['host'])
            || strtolower($base['scheme']) !== 'https'
            || strtolower($base['host']) !== $allowed_host
            || isset($base['user']) || isset($base['pass']) || isset($base['port'])
            || isset($base['query']) || isset($base['fragment'])
            || !empty($base['path'])) {
            return new WP_Error('sportiq_config', 'SportIQ API 주소 설정이 올바르지 않습니다.');
        }

        $response = wp_remote_get($this->api_base_url . $path, array(
            'timeout' => self::REQUEST_TIMEOUT,
            'redirection' => 0,
            'limit_response_size' => 65536,
            'headers' => array('Accept' => 'application/json'),
        ));
        if (is_wp_error($response)) {
            return new WP_Error('sportiq_transport', '예측 서버 연결에 실패했습니다.');
        }
        $status = (int) wp_remote_retrieve_response_code($response);
        if ($status === 404) {
            return new WP_Error('sportiq_not_found', '예측 데이터가 없습니다.');
        }
        if ($status !== 200) {
            return new WP_Error('sportiq_http', '예측 서버가 정상 응답하지 않았습니다.');
        }
        $data = json_decode(wp_remote_retrieve_body($response), true);
        if (!is_array($data) || !isset($data['match_id']) || !isset($data['probabilities'])) {
            return new WP_Error('sportiq_response', '예측 응답 형식이 올바르지 않습니다.');
        }
        return $data;
    }

    private function with_meta($data, $source, $stale) {
        unset($data['_meta']);
        $data['_meta'] = array('source' => $source, 'stale' => (bool) $stale);
        return $data;
    }

    private function format_error($message) {
        return array('status' => 'error', 'message' => $message);
    }
}
}
