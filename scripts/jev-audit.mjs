/**
 * ==============================================================================
 * Jev-1 System-1 Web Route & Operational Error Monitor
 * ==============================================================================
 * 
 * Performs high-speed (70-500ms) structured error classification on key routes
 * using TypeSafe AI's Jev System-1 model (`jev-1`).
 * 
 * Evaluates:
 * - Homepage (/)
 * - Categories (/categories)
 * - Products (/products)
 * - Contact (/contact)
 * - Sitemap (/sitemap.xml)
 * - Robots (/robots.txt)
 * - Portal (/portal)
 * - Health (/health)
 * 
 * Dispatches automated notifications via Resend API or Webhook if alerts trigger.
 * Features automatic heuristic fallback when TYPESAFE_API_KEY is unset.
 */

import { TypeSafeClient, choice, noul } from '@typesafe-ai/sdk';

const BASE_URL = (process.env.BASE_URL || 'http://localhost:8000').replace(/\/+$/, '');
const TYPESAFE_API_KEY = process.env.TYPESAFE_API_KEY;
const RESEND_API_KEY = process.env.RESEND_API_KEY;
const NOTIFICATION_EMAIL = process.env.NOTIFICATION_EMAIL || 'devops@medicalbilling.local';
const WEBHOOK_URL = process.env.WEBHOOK_URL;

const ROUTES_TO_AUDIT = [
  { path: '/', label: 'Homepage / Service Discovery' },
  { path: '/categories', label: 'Clinical Specialty Catalog' },
  { path: '/products', label: 'Module Capabilities Catalog' },
  { path: '/contact', label: 'Compliance & Support Contact' },
  { path: '/sitemap.xml', label: 'XML Sitemap Index' },
  { path: '/robots.txt', label: 'Robots Crawl Policy' },
  { path: '/portal', label: 'Medical Biller Web Portal' },
  { path: '/health', label: 'Core Service Health Probe' }
];

async function sendNotification(alertReport) {
  console.log('\n[ALERT DISPATCH] Generating automated notifications...');
  
  if (RESEND_API_KEY && NOTIFICATION_EMAIL) {
    try {
      const resp = await fetch('https://api.resend.com/emails', {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${RESEND_API_KEY}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          from: 'Jev Error Monitor <alerts@medicalbilling.local>',
          to: [NOTIFICATION_EMAIL],
          subject: `🚨 Jev-1 Alert: Route Degradation Detected on ${BASE_URL}`,
          text: JSON.stringify(alertReport, null, 2)
        })
      });
      console.log(`[ALERT DISPATCH] Resend email dispatched: status ${resp.status}`);
    } catch (err) {
      console.error(`[ALERT DISPATCH] Resend notification failed: ${err.message}`);
    }
  }

  if (WEBHOOK_URL) {
    try {
      const resp = await fetch(WEBHOOK_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          source: 'jev-audit',
          timestamp: new Date().toISOString(),
          report: alertReport
        })
      });
      console.log(`[ALERT DISPATCH] Webhook alert dispatched: status ${resp.status}`);
    } catch (err) {
      console.error(`[ALERT DISPATCH] Webhook dispatch failed: ${err.message}`);
    }
  }
}

async function auditRoute(client, route) {
  const targetUrl = `${BASE_URL}${route.path}`;
  const start = performance.now();
  let status = 0;
  let statusText = '';
  let bodyText = '';
  let contentType = '';
  let errorMsg = null;

  try {
    const res = await fetch(targetUrl, {
      method: 'GET',
      headers: {
        'User-Agent': 'Jev-Monitor/1.0 (TypeSafe-AI; +https://typesafe.ai)'
      },
      signal: AbortSignal.timeout(8000)
    });
    status = res.status;
    statusText = res.statusText;
    contentType = res.headers.get('content-type') || '';
    bodyText = await res.text();
  } catch (err) {
    errorMsg = err.message;
    status = 599;
    statusText = 'Connection or Timeout Failure';
  }

  const latencyMs = Math.round(performance.now() - start);
  const sample = (bodyText || errorMsg || '').slice(0, 400);

  // Classification using Jev System-1 or Heuristic Fallback
  let evaluation = null;

  if (client) {
    try {
      const evalResult = await client.systemOne({
        state: {
          url: targetUrl,
          route_label: route.label,
          status,
          statusText,
          latencyMs,
          contentType,
          bodySample: sample,
          error: errorMsg
        },
        questions: {
          is_operational: noul({
            instructions: "Is this web route operational and serving the expected HTTP response?"
          }),
          severity: choice({
            criteria: {
              none: "HTTP 200 with valid expected content, zero user impact",
              low: "Minor warning or benign redirect, performance acceptable",
              medium: "Degraded performance or formatting warning",
              high: "4xx client error or unhandled response",
              critical: "5xx server error, fatal crash, or unreachable service"
            }
          }),
          should_alert: noul({
            instructions: "Does this response require immediate alert dispatch to the on-call team?"
          })
        },
        model: 'jev-1'
      });

      evaluation = {
        is_operational: evalResult.answers.is_operational.noul > 0.5,
        operational_confidence: evalResult.answers.is_operational.noul,
        severity: evalResult.answers.severity.choice,
        should_alert: evalResult.answers.should_alert.noul > 0.5,
        alert_confidence: evalResult.answers.should_alert.noul,
        engine: 'jev-1 (System-1 Neural Inference)'
      };
    } catch (sdkErr) {
      console.warn(`[WARN] Jev-1 SDK inference error: ${sdkErr.message}. Falling back to heuristic classifier.`);
    }
  }

  // Heuristic Fallback Classifier
  if (!evaluation) {
    const is2xx = status >= 200 && status < 400;
    const isSeverelySlow = latencyMs > 3000;
    const severity = !is2xx
      ? (status >= 500 || status === 599 ? 'critical' : 'high')
      : (isSeverelySlow ? 'medium' : 'none');

    evaluation = {
      is_operational: is2xx,
      operational_confidence: is2xx ? 1.0 : 0.0,
      severity,
      should_alert: !is2xx || severity === 'critical',
      alert_confidence: !is2xx ? 1.0 : 0.0,
      engine: 'Heuristic Fallback Engine'
    };
  }

  return {
    path: route.path,
    label: route.label,
    targetUrl,
    status,
    latencyMs,
    evaluation
  };
}

async function runAudit() {
  console.log('='.repeat(78));
  console.log(`🔍 Jev-1 System-1 Web Audit: ${BASE_URL}`);
  console.log(`   Engine Mode: ${TYPESAFE_API_KEY ? 'TypeSafe Jev-1 System-1 Active' : 'Heuristic Fallback (TYPESAFE_API_KEY unset)'}`);
  console.log(`   Timestamp:   ${new Date().toISOString()}`);
  console.log('='.repeat(78));

  const client = TYPESAFE_API_KEY ? new TypeSafeClient({ apiKey: TYPESAFE_API_KEY }) : null;
  const results = [];
  let alertCount = 0;

  for (const route of ROUTES_TO_AUDIT) {
    process.stdout.write(`→ Testing ${route.path.padEnd(16)} `);
    const result = await auditRoute(client, route);
    results.push(result);

    const isOk = result.evaluation.is_operational && result.evaluation.severity === 'none';
    const statusIcon = isOk ? '✅ PASS' : '❌ ALERT';
    console.log(`[${statusIcon}] HTTP ${result.status} (${result.latencyMs}ms) - Severity: ${result.evaluation.severity}`);

    if (result.evaluation.should_alert) {
      alertCount++;
    }
  }

  console.log('\n' + '='.repeat(78));
  console.log('📊 AUDIT SUMMARY TABLE');
  console.log('='.repeat(78));
  console.log('Route              | HTTP | Latency | Operational | Severity | Alert');
  console.log('-'.repeat(78));
  for (const r of results) {
    const p = r.path.padEnd(18);
    const s = String(r.status).padEnd(4);
    const lat = `${r.latencyMs}ms`.padEnd(7);
    const op = (r.evaluation.is_operational ? 'YES' : 'NO').padEnd(11);
    const sev = r.evaluation.severity.padEnd(8);
    const al = r.evaluation.should_alert ? 'YES' : 'NO';
    console.log(`${p} | ${s} | ${lat} | ${op} | ${sev} | ${al}`);
  }
  console.log('='.repeat(78));

  if (alertCount > 0) {
    console.error(`\n🚨 AUDIT FAILED: ${alertCount} route(s) triggered alerts.`);
    await sendNotification({
      baseUrl: BASE_URL,
      alertCount,
      failedRoutes: results.filter(r => r.evaluation.should_alert)
    });
    process.exitCode = 1;
  } else {
    console.log(`\n🎉 AUDIT SUCCESSFUL: 0 errors reported across all ${results.length} audited routes.`);
    process.exitCode = 0;
  }
}

runAudit().catch(err => {
  console.error(`[FATAL] Jev audit runner failed: ${err.stack || err.message}`);
  process.exitCode = 1;
});
