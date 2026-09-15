/**
 * The only place in the app that knows the API's URL or shape.
 *
 * Every call goes through `request()`, which normalises failures into an
 * `ApiError` carrying a status code and a human-readable message, so callers
 * can distinguish "the backend is down" (status 0) from "that model does not
 * exist" (422) without parsing strings.
 */

const BASE_URL = (import.meta.env.VITE_API_URL || '').replace(/\/+$/, '');

export class ApiError extends Error {
  constructor(message, { status = 0, detail = null } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    /** A network-level failure: the API is unreachable, not merely unhappy. */
    this.isNetworkError = status === 0;
  }
}

function buildUrl(path, params = {}) {
  const url = new URL(`${BASE_URL}${path}`, window.location.origin);
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') {
      url.searchParams.set(key, String(value));
    }
  }
  return url.toString();
}

async function readErrorDetail(response) {
  try {
    const body = await response.json();
    if (typeof body?.detail === 'string') return body.detail;
    if (Array.isArray(body?.detail)) {
      return body.detail.map((item) => item.msg).filter(Boolean).join('; ');
    }
    if (typeof body?.detail?.msg === 'string') return body.detail.msg;
    if (typeof body?.error === 'string') return body.detail || body.error;
    return null;
  } catch {
    return null;
  }
}

async function request(path, { params, signal, method = 'GET', body } = {}) {
  let response;
  try {
    response = await fetch(buildUrl(path, params), {
      method,
      signal,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (error) {
    // An aborted request is a normal part of switching models; let callers
    // detect it rather than rendering it as a failure.
    if (error?.name === 'AbortError') throw error;
    throw new ApiError(
      'Could not reach the SignalStack API.',
      { status: 0, detail: error?.message ?? null },
    );
  }

  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new ApiError(
      detail || `Request failed with status ${response.status}.`,
      { status: response.status, detail },
    );
  }

  if (response.status === 204) return null;
  return response.json();
}

// --- endpoints -------------------------------------------------------------

export const getHealth = (signal) => request('/health', { signal });

export const getModels = (signal) => request('/api/models', { signal });

export const getSummary = (model, signal) =>
  request('/api/summary', { params: { model }, signal });

export const getChannels = (model, signal) =>
  request('/api/channels', { params: { model }, signal });

export const getModelComparison = (signal) =>
  request('/api/model-comparison', { signal });

export const getTimeseries = (model, granularity = 'day', signal) =>
  request('/api/timeseries', { params: { model, granularity }, signal });

export const getJourneys = (model, limit = 12, signal) =>
  request('/api/journeys', { params: { model, limit }, signal });

export const getPipelineHealth = (signal) =>
  request('/api/pipeline/health', { signal });

/** 202 with a job id, or an ApiError with status 409 if one is already running. */
export const startDemoReset = (payload, signal) =>
  request('/api/demo/reset', { method: 'POST', body: payload, signal });

export const getDemoStatus = (jobId, signal) =>
  request(`/api/demo/status/${encodeURIComponent(jobId)}`, { signal });

export { BASE_URL };
