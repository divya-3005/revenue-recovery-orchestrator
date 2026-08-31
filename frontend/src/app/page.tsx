"use client";

import { useEffect, useState, useCallback } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  RefreshCw, Play, Search, Activity, ShieldCheck,
  Users, CheckCircle2,
  Inbox, Minus, ArrowUpRight,
  CalendarClock, XCircle, RotateCw, AlertTriangle
} from "lucide-react";

// ── Types ────────────────────────────────────────────────────────────────

type CaseStatus = "open" | "in_progress" | "payment_pending" | "awaiting_approval" | "escalated" | "recovered" | "failed" | "closed";

interface RecoveryCase {
  id: string;
  customer_id: string;
  case_type: string;
  amount_paise: number;
  priority_score: number;
  status: CaseStatus;
  retry_count: number;
  follow_up_count: number;
  cumulative_discount_paise: number;
  latest_diagnosis_category: string | null;
  latest_diagnosis_reasoning: string | null;
  latest_action_recommended: string | null;
  latest_channel: string | null;
  latest_comms_preview: string | null;
  promise_to_pay_date: string | null;
  pending_decision_json: { recommended_action: string; reasoning: string; action_parameters?: Record<string, unknown> } | null;
  pending_decision_id?: string | null;
  pending_decision_hash?: string | null;
  // "pending" = this case is actually gated behind Approve/Reject (Feature
  // 15). A case can be status "escalated" (Feature 9 — handed to a human,
  // e.g. a hard decline) without ever having been approval-gated, in which
  // case this is null/"not_required" and Approve/Reject don't apply to it.
  approval_status?: string | null;
  created_at: string;
}

interface Analytics {
  total_cases: number;
  total_at_risk_paise: number;
  total_recovered_paise: number;
  total_discount_cost_paise: number;
  total_comms_cost_paise: number;
  net_recovered_paise: number;
  recovery_rate_percent: number;
  net_recovery_rate_percent: number;
  breakdown_by_case_type: Record<string, { total: number; recovered: number; at_risk_paise: number; recovered_paise: number }>;
  breakdown_by_channel: Record<string, { total: number; recovered: number; at_risk_paise: number; recovered_paise: number }>;
  breakdown_by_status: Record<string, number>;
  exceptions: { case_id: string; status: string; case_type: string; amount_paise: number }[];
}

interface AuditLog {
  id: string;
  action_type: string;
  description: string;
  reasoning: string;
  created_at: string;
}

interface PolicyConfig {
  max_retries: number;
  max_discount_percent: number;
  require_human_approval_above_paise: number;
  block_hard_declines: boolean;
  pre_debit_notice_hours?: number;
  max_days_pursued?: number;
}

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://127.0.0.1:8000/api/v1";

const formatINR = (paise: number) =>
  (paise / 100).toLocaleString("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });

const CASE_LABELS: Record<string, string> = {
  subscription_failed: "Subscription",
  checkout_abandoned: "Checkout Cart",
  invoice_overdue: "Overdue Invoice",
};

const STATUS_BADGE_STYLES: Record<string, string> = {
  recovered: "bg-emerald-500/10 text-emerald-400 border-emerald-500/30",
  payment_pending: "bg-amber-500/10 text-amber-300 border-amber-500/30",
  awaiting_approval: "bg-purple-500/10 text-purple-300 border-purple-500/30",
  escalated: "bg-rose-500/10 text-rose-300 border-rose-500/30",
  in_progress: "bg-blue-500/10 text-blue-300 border-blue-500/30",
  failed: "bg-red-500/10 text-red-400 border-red-500/30",
  closed: "bg-slate-500/10 text-slate-400 border-slate-500/30",
  open: "bg-slate-800 text-slate-300 border-slate-700",
};

// ── Main Dashboard ───────────────────────────────────────────────────────

export default function Dashboard() {
  const [tab, setTab] = useState<"queue" | "escalated" | "analytics">("queue");
  const [cases, setCases] = useState<RecoveryCase[]>([]);
  const [escalated, setEscalated] = useState<RecoveryCase[]>([]);
  const [analytics, setAnalytics] = useState<Analytics | null>(null);
  const [policy, setPolicy] = useState<PolicyConfig | null>(null);
  const [loading, setLoading] = useState(false);
  const [actionBusy, setActionBusy] = useState<string | null>(null);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [noticeMsg, setNoticeMsg] = useState<string | null>(null);

  const reportError = async (res: Response, fallback: string) => {
    let detail = fallback;
    try {
      const body = await res.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : fallback;
    } catch {
      // response wasn't JSON — stick with fallback
    }
    setErrorMsg(detail);
  };

  // Modals state
  const [auditCaseId, setAuditCaseId] = useState<string | null>(null);
  const [auditLogs, setAuditLogs] = useState<AuditLog[]>([]);
  const [auditLoading, setAuditLoading] = useState(false);

  const [commsModalMsg, setCommsModalMsg] = useState<string | null>(null);

  const [ptpCaseId, setPtpCaseId] = useState<string | null>(null);
  const [ptpDate, setPtpDate] = useState(() => {
    const d = new Date(); d.setDate(d.getDate() + 2);
    return d.toISOString().split("T")[0];
  });
  const [ptpNote, setPtpNote] = useState("");

  const refreshData = useCallback(async () => {
    try {
      const [cRes, eRes, aRes, pRes] = await Promise.all([
        fetch(`${API_BASE}/cases?limit=100`),
        fetch(`${API_BASE}/cases/escalated`),
        fetch(`${API_BASE}/analytics`),
        fetch(`${API_BASE}/policy`),
      ]);
      if (cRes.ok) setCases(await cRes.json());
      if (eRes.ok) setEscalated(await eRes.json());
      if (aRes.ok) setAnalytics(await aRes.json());
      if (pRes.ok) setPolicy(await pRes.json());
    } catch (e) {
      console.error("Data refresh failed", e);
    }
  }, []);

  useEffect(() => {
    refreshData();
    const timer = setInterval(refreshData, 4000);
    return () => clearInterval(timer);
  }, [refreshData]);

  // Actions
  const runBatch = async () => {
    setActionBusy("batch");
    setErrorMsg(null);
    try {
      const res = await fetch(`${API_BASE}/batch`, { method: "POST" });
      if (!res.ok) return reportError(res, "Failed to run the batch.");
      await refreshData();
    } catch {
      setErrorMsg("Couldn't reach the server to run the batch.");
    } finally {
      setActionBusy(null);
    }
  };

  const seedDemo = async () => {
    setActionBusy("demo");
    setErrorMsg(null);
    try {
      const res = await fetch(`${API_BASE}/demo/seed`, { method: "POST" });
      if (!res.ok) return reportError(res, "Failed to seed demo cases.");
      await refreshData();
    } catch {
      setErrorMsg("Couldn't reach the server to seed demo cases.");
    } finally {
      setActionBusy(null);
    }
  };

  const runFollowUps = async () => {
    setActionBusy("followups");
    setErrorMsg(null);
    setNoticeMsg(null);
    try {
      const res = await fetch(`${API_BASE}/jobs/run-follow-ups?force=true`, { method: "POST" });
      if (!res.ok) return reportError(res, "Failed to run follow-ups.");
      const data = await res.json();
      setNoticeMsg(
        data.cases_checked === 0
          ? "No unpaid cases to re-engage right now."
          : `Re-engaged ${data.cases_checked} unpaid case(s) with an escalated message.`
      );
      await refreshData();
    } catch {
      setErrorMsg("Couldn't reach the server to run follow-ups.");
    } finally {
      setActionBusy(null);
    }
  };

  const openAudit = async (caseId: string) => {
    setAuditCaseId(caseId);
    setAuditLoading(true);
    try {
      const res = await fetch(`${API_BASE}/cases/${caseId}/audit`);
      if (res.ok) {
        setAuditLogs(await res.json());
      } else {
        await reportError(res, "Failed to load the audit trail.");
      }
    } catch {
      setErrorMsg("Couldn't reach the server to load the audit trail.");
    } finally {
      setAuditLoading(false);
    }
  };

  const approveCase = async (c: RecoveryCase) => {
    setActionBusy(c.id);
    setErrorMsg(null);
    try {
      const res = await fetch(`${API_BASE}/cases/${c.id}/approve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          decision_id: c.pending_decision_id || "dec_human",
          decision_hash: c.pending_decision_hash || "hash_human",
          reviewer_id: "operations_admin",
        }),
      });
      if (!res.ok) return reportError(res, "Failed to approve this case.");
      await refreshData();
    } catch {
      setErrorMsg("Couldn't reach the server to approve this case.");
    } finally {
      setActionBusy(null);
    }
  };

  const rejectCase = async (c: RecoveryCase) => {
    setActionBusy(c.id);
    setErrorMsg(null);
    try {
      const res = await fetch(`${API_BASE}/cases/${c.id}/reject`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          decision_id: c.pending_decision_id || "dec_human",
          decision_hash: c.pending_decision_hash || "hash_human",
          reviewer_id: "operations_admin",
        }),
      });
      if (!res.ok) return reportError(res, "Failed to reject this case.");
      await refreshData();
    } catch {
      setErrorMsg("Couldn't reach the server to reject this case.");
    } finally {
      setActionBusy(null);
    }
  };

  const closeCase = async (caseId: string) => {
    setActionBusy(caseId);
    setErrorMsg(null);
    try {
      const res = await fetch(`${API_BASE}/cases/${caseId}/close`, { method: "POST" });
      if (!res.ok) return reportError(res, "Failed to close this case.");
      await refreshData();
    } catch {
      setErrorMsg("Couldn't reach the server to close this case.");
    } finally {
      setActionBusy(null);
    }
  };

  const submitPtp = async () => {
    if (!ptpCaseId || !ptpDate) return;
    setActionBusy("ptp");
    setErrorMsg(null);
    try {
      const res = await fetch(`${API_BASE}/cases/${ptpCaseId}/promise-to-pay`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ date: ptpDate, note: ptpNote || "Customer payment promise" }),
      });
      if (!res.ok) return reportError(res, "Failed to record the promise-to-pay.");
      setPtpCaseId(null);
      await refreshData();
    } catch {
      setErrorMsg("Couldn't reach the server to record the promise-to-pay.");
    } finally {
      setActionBusy(null);
    }
  };

  return (
    <div className="min-h-screen bg-[#070913] text-slate-100 font-sans pb-16 selection:bg-indigo-500/30">
      {/* Background glow */}
      <div className="fixed inset-0 overflow-hidden pointer-events-none">
        <div className="absolute -top-40 left-1/4 w-96 h-96 bg-indigo-600/20 rounded-full blur-[140px]" />
        <div className="absolute top-1/3 -right-20 w-96 h-96 bg-purple-600/15 rounded-full blur-[140px]" />
        <div className="absolute bottom-10 left-10 w-96 h-96 bg-emerald-600/10 rounded-full blur-[140px]" />
      </div>

      <div className="relative max-w-7xl mx-auto px-6 pt-8 space-y-6">
        {/* Top Header */}
        <header className="flex flex-wrap items-center justify-between gap-4 p-5 rounded-2xl bg-white/[0.03] border border-white/10 backdrop-blur-xl shadow-2xl">
          <div className="flex items-center gap-3.5">
            <div className="p-2.5 rounded-xl bg-gradient-to-tr from-indigo-500 to-purple-500 shadow-lg shadow-indigo-500/25">
              <Activity className="w-6 h-6 text-white" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h1 className="text-xl font-bold tracking-tight text-white">Revenue Recovery Orchestrator</h1>
                <Badge variant="outline" className="bg-emerald-500/10 text-emerald-400 border-emerald-500/30 text-[10px] tracking-wide">
                  AUTONOMOUS
                </Badge>
              </div>
              <p className="text-xs text-slate-400">Subscriptions · Abandoned Checkouts · Invoices — One Decision Engine</p>
            </div>
          </div>

          <div className="flex items-center gap-2.5">
            <Button
              variant="outline"
              size="sm"
              onClick={() => { setLoading(true); refreshData().finally(() => setLoading(false)); }}
              disabled={loading}
              className="bg-white/5 border-white/10 text-slate-300 hover:bg-white/10"
            >
              <RefreshCw className={`w-3.5 h-3.5 mr-1.5 ${loading ? "animate-spin" : ""}`} /> Refresh
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={seedDemo}
              disabled={actionBusy === "demo"}
              className="bg-purple-500/10 border-purple-500/30 text-purple-300 hover:bg-purple-500/20"
            >
              <Play className="w-3.5 h-3.5 mr-1.5" /> {actionBusy === "demo" ? "Seeding…" : "Seed 5 Pitch Demos"}
            </Button>
            <Button
              size="sm"
              onClick={runBatch}
              disabled={actionBusy === "batch"}
              className="bg-indigo-600 hover:bg-indigo-500 text-white shadow-lg shadow-indigo-600/30"
            >
              <Play className="w-3.5 h-3.5 mr-1.5" /> {actionBusy === "batch" ? "Processing Batch…" : "Run 50+ Case Batch"}
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={runFollowUps}
              disabled={actionBusy === "followups"}
              title="Re-engage any unpaid PAYMENT_PENDING case immediately (bypasses the 48h window for demo purposes)"
              className="bg-white/5 border-white/10 text-slate-300 hover:bg-white/10"
            >
              <RotateCw className="w-3.5 h-3.5 mr-1.5" /> {actionBusy === "followups" ? "Checking…" : "Re-engage Unpaid (Demo)"}
            </Button>
          </div>
        </header>

        {errorMsg && (
          <div className="flex items-start gap-2.5 p-3.5 rounded-xl bg-rose-500/10 border border-rose-500/30 text-rose-200 text-sm">
            <AlertTriangle className="w-4 h-4 mt-0.5 flex-shrink-0 text-rose-400" />
            <p className="flex-1">{errorMsg}</p>
            <button onClick={() => setErrorMsg(null)} className="text-rose-400 hover:text-rose-200">
              <XCircle className="w-4 h-4" />
            </button>
          </div>
        )}

        {noticeMsg && (
          <div className="flex items-start gap-2.5 p-3.5 rounded-xl bg-emerald-500/10 border border-emerald-500/30 text-emerald-200 text-sm">
            <CheckCircle2 className="w-4 h-4 mt-0.5 flex-shrink-0 text-emerald-400" />
            <p className="flex-1">{noticeMsg}</p>
            <button onClick={() => setNoticeMsg(null)} className="text-emerald-400 hover:text-emerald-200">
              <XCircle className="w-4 h-4" />
            </button>
          </div>
        )}

        {/* KPI Strip */}
        <section className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <Card className="bg-white/[0.02] border-white/5">
            <CardHeader className="p-4 pb-1">
              <CardTitle className="text-xs font-semibold uppercase tracking-wider text-slate-400 flex items-center gap-1.5">
                <Users className="w-3.5 h-3.5 text-indigo-400" /> Total Revenue at Risk
              </CardTitle>
            </CardHeader>
            <CardContent className="p-4 pt-1">
              <p className="text-2xl font-bold text-rose-400">{analytics ? formatINR(analytics.total_at_risk_paise) : "₹0"}</p>
              <p className="text-[11px] text-slate-500 mt-0.5">{analytics?.total_cases ?? 0} active/historical cases</p>
            </CardContent>
          </Card>

          <Card className="bg-white/[0.02] border-white/5">
            <CardHeader className="p-4 pb-1">
              <CardTitle className="text-xs font-semibold uppercase tracking-wider text-slate-400 flex items-center gap-1.5">
                <ShieldCheck className="w-3.5 h-3.5 text-emerald-400" /> Gross Recovered
              </CardTitle>
            </CardHeader>
            <CardContent className="p-4 pt-1">
              <p className="text-2xl font-bold text-emerald-400">{analytics ? formatINR(analytics.total_recovered_paise) : "₹0"}</p>
              <p className="text-[11px] text-emerald-500/80 mt-0.5">{analytics?.recovery_rate_percent ?? 0}% recovery rate</p>
            </CardContent>
          </Card>

          <Card className="bg-white/[0.02] border-white/5">
            <CardHeader className="p-4 pb-1">
              <CardTitle className="text-xs font-semibold uppercase tracking-wider text-slate-400 flex items-center gap-1.5">
                <Minus className="w-3.5 h-3.5 text-amber-400" /> Discounts & Comms
              </CardTitle>
            </CardHeader>
            <CardContent className="p-4 pt-1">
              <p className="text-2xl font-bold text-amber-400">
                {analytics ? formatINR((analytics.total_discount_cost_paise || 0) + (analytics.total_comms_cost_paise || 0)) : "₹0"}
              </p>
              <p className="text-[11px] text-slate-500 mt-0.5">Discounts: {formatINR(analytics?.total_discount_cost_paise || 0)}</p>
            </CardContent>
          </Card>

          <Card className="bg-indigo-950/30 border-indigo-500/20">
            <CardHeader className="p-4 pb-1">
              <CardTitle className="text-xs font-semibold uppercase tracking-wider text-indigo-300 flex items-center gap-1.5">
                <ArrowUpRight className="w-3.5 h-3.5 text-indigo-400" /> Net Recovered Value
              </CardTitle>
            </CardHeader>
            <CardContent className="p-4 pt-1">
              <p className="text-2xl font-bold text-indigo-300">{analytics ? formatINR(analytics.net_recovered_paise) : "₹0"}</p>
              <p className="text-[11px] text-indigo-400/80 mt-0.5">{analytics?.net_recovery_rate_percent ?? 0}% net recovered</p>
            </CardContent>
          </Card>
        </section>

        {/* Tab Navigation */}
        <div className="flex border-b border-white/10 gap-2">
          {(
            [
              { id: "queue", label: "Live Queue", count: cases.length },
              { id: "escalated", label: "Escalation & Approvals", count: escalated.length },
              { id: "analytics", label: "Analytics & Economics" },
            ] as Array<{ id: "queue" | "escalated" | "analytics"; label: string; count?: number }>
          ).map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`pb-3 px-4 text-sm font-medium transition-colors border-b-2 flex items-center gap-2 ${
                tab === t.id
                  ? "border-indigo-500 text-indigo-300"
                  : "border-transparent text-slate-400 hover:text-slate-200"
              }`}
            >
              {t.label}
              {t.count !== undefined && (
                <span className={`text-[11px] px-2 py-0.5 rounded-full ${tab === t.id ? "bg-indigo-500/20 text-indigo-300" : "bg-white/5 text-slate-400"}`}>
                  {t.count}
                </span>
              )}
            </button>
          ))}
        </div>

        {/* ── TAB 1: LIVE QUEUE ── */}
        {tab === "queue" && (
          <Card className="bg-white/[0.02] border-white/10 overflow-hidden">
            <Table>
              <TableHeader>
                <TableRow className="border-white/10 hover:bg-transparent bg-white/[0.01]">
                  <TableHead className="text-slate-400 text-xs">Case / Customer</TableHead>
                  <TableHead className="text-slate-400 text-xs">Type</TableHead>
                  <TableHead className="text-slate-400 text-xs">AI Diagnosis</TableHead>
                  <TableHead className="text-slate-400 text-xs">Next Action</TableHead>
                  <TableHead className="text-slate-400 text-xs">Amount</TableHead>
                  <TableHead className="text-slate-400 text-xs">Expected Value</TableHead>
                  <TableHead className="text-slate-400 text-xs">Status</TableHead>
                  <TableHead className="text-slate-400 text-xs text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {cases.length === 0 ? (
                  <TableRow>
                    <TableCell colSpan={8} className="text-center py-12 text-slate-500 text-sm">
                      No recovery cases found. Click &quot;Run 50+ Case Batch&quot; or &quot;Seed 5 Pitch Demos&quot; to begin.
                    </TableCell>
                  </TableRow>
                ) : (
                  cases.map((c) => (
                    <TableRow key={c.id} className="border-white/5 hover:bg-white/[0.02] transition-colors">
                      <TableCell>
                        <div className="font-mono text-xs text-slate-400">{c.id.slice(0, 8)}…</div>
                        <div className="text-xs font-semibold text-slate-200">{c.customer_id}</div>
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline" className="bg-white/5 text-slate-300 border-white/10 text-[11px]">
                          {CASE_LABELS[c.case_type] || c.case_type}
                        </Badge>
                      </TableCell>
                      <TableCell className="max-w-[180px]">
                        <div className="text-xs font-medium text-slate-200 truncate capitalize">
                          {c.latest_diagnosis_category ? c.latest_diagnosis_category.replace(/_/g, " ") : "—"}
                        </div>
                        <div className="text-[11px] text-slate-400 truncate">{c.latest_diagnosis_reasoning || "Analyzing..."}</div>
                      </TableCell>
                      <TableCell className="text-xs font-mono text-indigo-300">
                        {c.latest_action_recommended ? c.latest_action_recommended.replace(/_/g, " ") : "—"}
                      </TableCell>
                      <TableCell className="text-xs font-semibold text-slate-200">{formatINR(c.amount_paise)}</TableCell>
                      <TableCell className="text-xs font-mono text-emerald-400">{formatINR(c.priority_score)}</TableCell>
                      <TableCell>
                        <Badge variant="outline" className={`${STATUS_BADGE_STYLES[c.status] || ""} text-[10px] uppercase font-mono`}>
                          {c.status.replace(/_/g, " ")}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-right space-x-1">
                        {c.promise_to_pay_date && (
                          <Badge variant="outline" className="bg-teal-500/10 text-teal-300 border-teal-500/30 text-[10px]">
                            PTP {c.promise_to_pay_date}
                          </Badge>
                        )}
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => { setPtpCaseId(c.id); }}
                          title="Capture Promise to Pay"
                          className="h-7 w-7 p-0 text-slate-400 hover:text-teal-300 hover:bg-teal-500/10"
                        >
                          <CalendarClock className="w-3.5 h-3.5" />
                        </Button>
                        {c.latest_comms_preview && (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => setCommsModalMsg(c.latest_comms_preview)}
                            title="Preview Customer Message"
                            className="h-7 w-7 p-0 text-slate-400 hover:text-indigo-300 hover:bg-indigo-500/10"
                          >
                            <Inbox className="w-3.5 h-3.5" />
                          </Button>
                        )}
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => openAudit(c.id)}
                          title="View Full Decision Trace"
                          className="h-7 w-7 p-0 text-slate-400 hover:text-white hover:bg-white/10"
                        >
                          <Search className="w-3.5 h-3.5" />
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))
                )}
              </TableBody>
            </Table>
          </Card>
        )}

        {/* ── TAB 2: ESCALATED & HUMAN APPROVAL ── */}
        {tab === "escalated" && (
          <div className="space-y-4">
            {escalated.length === 0 ? (
              <Card className="bg-white/[0.02] border-white/10 p-12 text-center text-slate-400">
                <CheckCircle2 className="w-8 h-8 text-emerald-400 mx-auto mb-2 opacity-80" />
                <p className="text-sm">No cases currently require human approval or escalation.</p>
              </Card>
            ) : (
              escalated.map((c) => (
                <Card key={c.id} className="bg-white/[0.02] border-white/10 p-5 rounded-2xl flex flex-wrap items-center justify-between gap-4">
                  <div className="space-y-1.5 max-w-xl">
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-xs text-slate-400">{c.id.slice(0, 8)}…</span>
                      <span className="text-sm font-bold text-white">{c.customer_id}</span>
                      <Badge variant="outline" className={`${STATUS_BADGE_STYLES[c.status] || ""} text-[10px]`}>
                        {c.status.replace(/_/g, " ")}
                      </Badge>
                      <span className="text-sm font-semibold text-rose-300">{formatINR(c.amount_paise)}</span>
                    </div>
                    <p className="text-xs text-slate-300">
                      <span className="text-indigo-400 font-medium">AI Recommendation:</span>{" "}
                      {c.pending_decision_json?.recommended_action.replace(/_/g, " ") || c.latest_action_recommended || "Human review requested"}
                    </p>
                    <p className="text-xs text-slate-400 italic">
                      &quot;{c.pending_decision_json?.reasoning || c.latest_diagnosis_reasoning || "Awaiting manager sign-off for high value transaction."}&quot;
                    </p>
                    {c.approval_status !== "pending" && (
                      <p className="text-[11px] text-slate-500">
                        Escalated for review — this case wasn&apos;t gated behind an approval, so there&apos;s no pending decision to approve or reject. Close it once handled.
                      </p>
                    )}
                  </div>

                  <div className="flex items-center gap-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => openAudit(c.id)}
                      className="bg-white/5 border-white/10 text-slate-300 hover:bg-white/10"
                    >
                      <Search className="w-3.5 h-3.5 mr-1.5" /> Trace
                    </Button>
                    {c.approval_status === "pending" ? (
                      <>
                        <Button
                          size="sm"
                          onClick={() => approveCase(c)}
                          disabled={actionBusy === c.id}
                          className="bg-emerald-600 hover:bg-emerald-500 text-white"
                        >
                          <CheckCircle2 className="w-3.5 h-3.5 mr-1.5" /> Approve &amp; Execute
                        </Button>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => rejectCase(c)}
                          disabled={actionBusy === c.id}
                          className="bg-rose-500/10 border-rose-500/30 text-rose-300 hover:bg-rose-500/20"
                        >
                          <XCircle className="w-3.5 h-3.5 mr-1.5" /> Reject
                        </Button>
                      </>
                    ) : (
                      <Badge variant="outline" className="bg-white/5 border-white/10 text-slate-400 text-[10px]">
                        No approval gate
                      </Badge>
                    )}
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => closeCase(c.id)}
                      disabled={actionBusy === c.id}
                      className="text-slate-400 hover:text-white"
                    >
                      Close
                    </Button>
                  </div>
                </Card>
              ))
            )}
          </div>
        )}

        {/* ── TAB 3: ANALYTICS & ECONOMICS ── */}
        {tab === "analytics" && analytics && (
          <div className="space-y-6">
            {/* Case Type Breakdown */}
            <Card className="bg-white/[0.02] border-white/10">
              <CardHeader className="p-5 border-b border-white/5">
                <CardTitle className="text-sm font-semibold text-white">Recovery Performance by Case Type</CardTitle>
              </CardHeader>
              <CardContent className="p-0">
                <Table>
                  <TableHeader>
                    <TableRow className="border-white/5 hover:bg-transparent">
                      <TableHead className="text-slate-400 text-xs">Signal Type</TableHead>
                      <TableHead className="text-slate-400 text-xs">Total Cases</TableHead>
                      <TableHead className="text-slate-400 text-xs">Recovered Cases</TableHead>
                      <TableHead className="text-slate-400 text-xs">At Risk</TableHead>
                      <TableHead className="text-slate-400 text-xs">Recovered Value</TableHead>
                      <TableHead className="text-slate-400 text-xs">Success Rate</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {Object.entries(analytics.breakdown_by_case_type).map(([key, data]) => {
                      const rate = data.total > 0 ? Math.round((data.recovered / data.total) * 100) : 0;
                      return (
                        <TableRow key={key} className="border-white/5">
                          <TableCell className="text-xs font-medium text-white">{CASE_LABELS[key] || key}</TableCell>
                          <TableCell className="text-xs font-mono text-slate-300">{data.total}</TableCell>
                          <TableCell className="text-xs font-mono text-emerald-400">{data.recovered}</TableCell>
                          <TableCell className="text-xs font-semibold text-rose-300">{formatINR(data.at_risk_paise)}</TableCell>
                          <TableCell className="text-xs font-semibold text-emerald-300">{formatINR(data.recovered_paise)}</TableCell>
                          <TableCell>
                            <Badge variant="outline" className="bg-emerald-500/10 text-emerald-300 border-emerald-500/30 text-[10px]">
                              {rate}%
                            </Badge>
                          </TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </CardContent>
            </Card>

            {/* Channel Breakdown */}
            {analytics.breakdown_by_channel && Object.keys(analytics.breakdown_by_channel).length > 0 && (
              <Card className="bg-white/[0.02] border-white/10">
                <CardHeader className="p-5 border-b border-white/5">
                  <CardTitle className="text-sm font-semibold text-white">Recovery Performance by Channel</CardTitle>
                </CardHeader>
                <CardContent className="p-0">
                  <Table>
                    <TableHeader>
                      <TableRow className="border-white/5 hover:bg-transparent">
                        <TableHead className="text-slate-400 text-xs">Channel</TableHead>
                        <TableHead className="text-slate-400 text-xs">Total</TableHead>
                        <TableHead className="text-slate-400 text-xs">Recovered</TableHead>
                        <TableHead className="text-slate-400 text-xs">At Risk</TableHead>
                        <TableHead className="text-slate-400 text-xs">Recovered ₹</TableHead>
                        <TableHead className="text-slate-400 text-xs">Rate</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {Object.entries(analytics.breakdown_by_channel).map(([channel, data]) => {
                        const rate = data.total > 0 ? Math.round((data.recovered / data.total) * 100) : 0;
                        return (
                          <TableRow key={channel} className="border-white/5">
                            <TableCell className="text-xs font-medium text-white uppercase">{channel}</TableCell>
                            <TableCell className="text-xs font-mono text-slate-300">{data.total}</TableCell>
                            <TableCell className="text-xs font-mono text-emerald-400">{data.recovered}</TableCell>
                            <TableCell className="text-xs font-semibold text-rose-300">{formatINR(data.at_risk_paise)}</TableCell>
                            <TableCell className="text-xs font-semibold text-emerald-300">{formatINR(data.recovered_paise)}</TableCell>
                            <TableCell>
                              <Badge variant="outline" className="bg-emerald-500/10 text-emerald-300 border-emerald-500/30 text-[10px]">
                                {rate}%
                              </Badge>
                            </TableCell>
                          </TableRow>
                        );
                      })}
                    </TableBody>
                  </Table>
                </CardContent>
              </Card>
            )}

            {/* Exception List (Feature 12: Honest Exception Accounting) */}
            {analytics.exceptions && analytics.exceptions.length > 0 && (
              <Card className="bg-white/[0.02] border-white/10">
                <CardHeader className="p-5 border-b border-white/5 flex flex-row items-center justify-between">
                  <CardTitle className="text-sm font-semibold text-white">Exception List (Unresolved &amp; Escalated Cases)</CardTitle>
                  <Badge variant="outline" className="bg-rose-500/10 text-rose-300 border-rose-500/30 text-xs">
                    {analytics.exceptions.length} cases
                  </Badge>
                </CardHeader>
                <CardContent className="p-0">
                  <Table>
                    <TableHeader>
                      <TableRow className="border-white/5 hover:bg-transparent">
                        <TableHead className="text-slate-400 text-xs">Case ID</TableHead>
                        <TableHead className="text-slate-400 text-xs">Type</TableHead>
                        <TableHead className="text-slate-400 text-xs">Status</TableHead>
                        <TableHead className="text-slate-400 text-xs text-right">Amount</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {analytics.exceptions.slice(0, 10).map((ex) => (
                        <TableRow key={ex.case_id} className="border-white/5">
                          <TableCell className="text-xs font-mono text-slate-400">{ex.case_id.slice(0, 8)}…</TableCell>
                          <TableCell className="text-xs text-slate-300">{CASE_LABELS[ex.case_type] || ex.case_type}</TableCell>
                          <TableCell>
                            <Badge variant="outline" className={`${STATUS_BADGE_STYLES[ex.status] || ""} text-[10px] uppercase`}>
                              {ex.status.replace(/_/g, " ")}
                            </Badge>
                          </TableCell>
                          <TableCell className="text-xs font-semibold text-rose-300 text-right">{formatINR(ex.amount_paise)}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </CardContent>
              </Card>
            )}

            {/* Policy Configuration Box */}
            {policy && (
              <Card className="bg-white/[0.02] border-white/10 p-5 rounded-2xl">
                <div className="flex items-center gap-2 mb-4">
                  <ShieldCheck className="w-4 h-4 text-indigo-400" />
                  <h3 className="text-sm font-semibold text-white">Active Deterministic Guardrails (Policy Layer)</h3>
                </div>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
                  <div className="p-3 bg-white/5 rounded-xl border border-white/5">
                    <span className="text-slate-400 block mb-1">Max Retry Cap:</span>
                    <span className="text-sm font-bold text-indigo-300">{policy.max_retries} attempts</span>
                  </div>
                  <div className="p-3 bg-white/5 rounded-xl border border-white/5">
                    <span className="text-slate-400 block mb-1">Discount Ceiling:</span>
                    <span className="text-sm font-bold text-amber-300">{policy.max_discount_percent}% max</span>
                  </div>
                  <div className="p-3 bg-white/5 rounded-xl border border-white/5">
                    <span className="text-slate-400 block mb-1">Human Approval Threshold:</span>
                    <span className="text-sm font-bold text-rose-300">{formatINR(policy.require_human_approval_above_paise)}</span>
                  </div>
                  <div className="p-3 bg-white/5 rounded-xl border border-white/5">
                    <span className="text-slate-400 block mb-1">Hard Decline Handling:</span>
                    <span className="text-sm font-bold text-emerald-300">Escalate, Never Retry</span>
                  </div>
                </div>
              </Card>
            )}
          </div>
        )}
      </div>

      {/* ── MODAL: AUDIT TRACE ── */}
      <Dialog open={!!auditCaseId} onOpenChange={(open) => !open && setAuditCaseId(null)}>
        <DialogContent className="bg-[#0b0e1b] border-white/10 text-slate-100 max-w-2xl max-h-[80vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="text-base font-bold text-white flex items-center gap-2">
              <Activity className="w-4 h-4 text-indigo-400" /> Decision Trace &amp; Audit Trail
              <span className="text-xs font-mono text-slate-400 ml-auto">{auditCaseId?.slice(0, 12)}…</span>
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-4 pt-2">
            {auditLoading ? (
              <div className="py-8 text-center text-slate-400 text-xs">Loading audit trail…</div>
            ) : auditLogs.length === 0 ? (
              <div className="py-8 text-center text-slate-500 text-xs">No audit logs recorded for this case.</div>
            ) : (
              <div className="border-l-2 border-indigo-500/30 pl-4 space-y-4 ml-2">
                {auditLogs.map((log) => (
                  <div key={log.id} className="relative space-y-1">
                    <div className="absolute -left-[21px] top-1 w-2.5 h-2.5 rounded-full bg-indigo-500 ring-4 ring-[#0b0e1b]" />
                    <div className="flex items-center justify-between">
                      <span className="text-xs font-bold text-indigo-300 uppercase tracking-wide">{log.action_type.replace(/_/g, " ")}</span>
                      <span className="text-[10px] text-slate-500 font-mono">
                        {new Date(log.created_at).toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" })}
                      </span>
                    </div>
                    <p className="text-xs text-slate-200">{log.description}</p>
                    {log.reasoning && (
                      <p className="text-[11px] text-slate-400 bg-black/30 p-2 rounded-lg font-mono whitespace-pre-wrap">
                        {log.reasoning}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </DialogContent>
      </Dialog>

      {/* ── MODAL: CUSTOMER COMMS PREVIEW ── */}
      <Dialog open={!!commsModalMsg} onOpenChange={(open) => !open && setCommsModalMsg(null)}>
        <DialogContent className="bg-[#0b0e1b] border-white/10 text-slate-100 max-w-md">
          <DialogHeader>
            <DialogTitle className="text-sm font-bold text-white flex items-center gap-2">
              <Inbox className="w-4 h-4 text-indigo-400" /> Generated Recovery Communication
            </DialogTitle>
          </DialogHeader>
          <div className="p-4 rounded-xl bg-white/[0.02] border border-white/5 text-xs text-slate-300 whitespace-pre-wrap font-sans leading-relaxed">
            {commsModalMsg}
          </div>
        </DialogContent>
      </Dialog>

      {/* ── MODAL: PROMISE TO PAY ── */}
      <Dialog open={!!ptpCaseId} onOpenChange={(open) => !open && setPtpCaseId(null)}>
        <DialogContent className="bg-[#0b0e1b] border-white/10 text-slate-100 max-w-md">
          <DialogHeader>
            <DialogTitle className="text-sm font-bold text-white flex items-center gap-2">
              <CalendarClock className="w-4 h-4 text-teal-400" /> Capture Promise-to-Pay
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-3 pt-2">
            <p className="text-xs text-slate-400">
              Customer commitment suspends automatic recovery actions until the promised date. If broken, auto-escalation triggers.
            </p>
            <div className="space-y-1">
              <label className="text-[11px] uppercase tracking-wider text-slate-400">Promise Date</label>
              <input
                type="date"
                value={ptpDate}
                onChange={(e) => setPtpDate(e.target.value)}
                className="w-full px-3 py-2 bg-white/5 border border-white/10 rounded-lg text-xs text-white focus:outline-none focus:border-teal-500"
              />
            </div>
            <div className="space-y-1">
              <label className="text-[11px] uppercase tracking-wider text-slate-400">Customer Note</label>
              <input
                type="text"
                placeholder="e.g. Will pay after invoice sign-off on Friday"
                value={ptpNote}
                onChange={(e) => setPtpNote(e.target.value)}
                className="w-full px-3 py-2 bg-white/5 border border-white/10 rounded-lg text-xs text-white focus:outline-none focus:border-teal-500"
              />
            </div>
            <div className="flex justify-end gap-2 pt-2">
              <Button variant="ghost" size="sm" onClick={() => setPtpCaseId(null)}>Cancel</Button>
              <Button size="sm" onClick={submitPtp} disabled={actionBusy === "ptp"} className="bg-teal-600 hover:bg-teal-500 text-white">
                Save Commitment
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
