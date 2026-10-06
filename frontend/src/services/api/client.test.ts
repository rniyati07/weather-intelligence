import { AxiosError, AxiosHeaders, type InternalAxiosRequestConfig } from 'axios'
import { describe, expect, it } from 'vitest'

import { getErrorPresentation } from '@/constants/error-copy'
import { apiClient } from './client'
import { ApiError } from './errors'

/** Make the next request fail the way axios reports it, with no network. */
function failWith(status: number | null, data?: unknown): Promise<unknown> {
  return apiClient
    .get('/probe', {
      adapter: (config) => {
        const request = { ...config, headers: AxiosHeaders.from(config.headers) }
        const response =
          status === null
            ? undefined
            : {
                status,
                statusText: '',
                data,
                headers: {},
                config: request as InternalAxiosRequestConfig,
              }
        return Promise.reject(
          new AxiosError(
            'failed',
            status === null ? 'ERR_NETWORK' : 'ERR_BAD_RESPONSE',
            request as InternalAxiosRequestConfig,
            null,
            response,
          ),
        )
      },
    })
    .then(
      () => {
        throw new Error('expected the request to fail')
      },
      (error: unknown) => error,
    )
}

const metadata = { apiVersion: '1.0', generatedAt: '2026-10-02T00:00:00Z', requestId: 'req_1' }

describe('response error mapping', () => {
  it('maps a missing/invalid API key to AUTHENTICATION_ERROR, not NOT_FOUND', async () => {
    const error = await failWith(401, {
      success: false,
      data: null,
      metadata,
      error: { code: 'AUTHENTICATION_ERROR', message: 'bad key', details: null },
    })
    expect(ApiError.is(error) && error.code).toBe('AUTHENTICATION_ERROR')
  })

  it('maps an envelope-less 401 to AUTHENTICATION_ERROR', async () => {
    const error = await failWith(401, '')
    expect(ApiError.is(error) && error.code).toBe('AUTHENTICATION_ERROR')
  })

  it('treats an envelope-less 502/503/504 (proxy or gateway down) as a connection failure', async () => {
    for (const status of [502, 503, 504]) {
      const error = await failWith(status, '')
      expect(ApiError.is(error) && error.code).toBe('NETWORK_ERROR')
    }
  })

  it('keeps an enveloped 503 as a provider outage', async () => {
    const error = await failWith(503, {
      success: false,
      data: null,
      metadata,
      error: { code: 'PROVIDER_UNAVAILABLE', message: 'down', details: null },
    })
    expect(ApiError.is(error) && error.code).toBe('PROVIDER_UNAVAILABLE')
  })

  it('maps a backend 500 to INTERNAL_ERROR', async () => {
    const error = await failWith(500, '')
    expect(ApiError.is(error) && error.code).toBe('INTERNAL_ERROR')
  })

  it('maps no response at all to NETWORK_ERROR', async () => {
    const error = await failWith(null)
    expect(ApiError.is(error) && error.code).toBe('NETWORK_ERROR')
  })
})

describe('error copy', () => {
  it('does not blame the destination for a NOT_FOUND', () => {
    expect(getErrorPresentation('NOT_FOUND').body).not.toMatch(/destination/i)
  })

  it('describes authentication failures as a configuration problem', () => {
    expect(getErrorPresentation('AUTHENTICATION_ERROR').body).toMatch(/authenticate/i)
  })
})
