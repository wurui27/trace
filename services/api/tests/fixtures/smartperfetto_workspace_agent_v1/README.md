# SmartPerfetto workspace-agent-v1 fixtures

Upstream: Gracker/SmartPerfetto
Tag: v1.0.38
Commit: 1508f99788bfcf18cc861e4bf4f8b472e84240c3
Contract owner: PerfPilot
Contract name: workspace-agent-v1
Upstream handshake: none

All identifiers, timestamps, text, paths, and credential-shaped values are synthetic. The fixtures retain only fields needed to freeze PerfPilot's consumer contract.

| Fixture | Reviewed upstream source at the pinned commit |
| --- | --- |
| `workspace-create-request.json` | `backend/src/routes/__tests__/enterpriseTenantRoutes.test.ts:232-245`; `backend/src/services/enterpriseAdminControlPlaneService.ts:232-261` |
| `workspace-create-success.json` | `backend/src/services/enterpriseAdminControlPlaneService.ts:232-261` |
| `workspace-list-success.json` | `backend/src/routes/enterpriseTenantRoutes.ts:114-136`; `backend/src/services/enterpriseAdminControlPlaneService.ts:225-230` |
| `trace-upload-success.json` | `backend/src/routes/simpleTraceRoutes.ts:763-830` |
| `trace-upload-success-false.json` | `backend/src/routes/simpleTraceRoutes.ts:288-297`; `backend/src/routes/simpleTraceRoutes.ts:763-830` |
| `analyze-smart-preview-request.json` | `backend/src/routes/agent/normalizeAnalyzeOptions.ts:63-91`; `backend/src/routes/agentRoutes.ts:1888-1965` |
| `analyze-smart-deep-dive-request.json` | `backend/src/agent/scene/types.ts:243-255`; `backend/src/routes/agent/normalizeAnalyzeOptions.ts:164-218` |
| `analyze-success.json` | `backend/src/routes/agentRoutes.ts:1888-1965`; `backend/src/routes/agentRoutes.ts:2140-2203` |
| `concurrent-quota.json` | `backend/src/routes/agentRoutes.ts:234-243`; `backend/src/services/enterpriseQuotaPolicyService.ts:283-299` |
| `monthly-quota.json` | `backend/src/routes/agentRoutes.ts:234-243`; `backend/src/services/enterpriseQuotaPolicyService.ts:263-279` |
| `progress-stream.sse` | `backend/src/routes/agentRoutes.ts:2213-2240`; `backend/src/routes/agentRoutes.ts:6570-6633` |
| `smart-preview-stream.sse` | `backend/src/routes/agentRoutes.ts:6586-6633`; `backend/src/agent/scene/buildSmartChatReport.ts:99-128` |
| `status-completed.json` | `backend/src/routes/agentRoutes.ts:2447-2528` |
| `resume-success.json` | `backend/src/routes/agentResumeRoutes.ts:57-105`; `backend/src/routes/agentResumeRoutes.ts:300-340` |
| `cancel-success.json` | `backend/src/routes/agentRoutes.ts:2839-2848` |
| `report-completed.json` | `backend/src/routes/agentReportRoutes.ts:21-127` |
