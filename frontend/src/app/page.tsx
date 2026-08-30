"use client";

import { useEffect, useState, useCallback } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  RefreshCw, Play, Search, AlertCircle, Activity, ShieldCheck,
  TrendingUp, ChevronRight, Users, CheckCircle2,
  BarChart3, Inbox, AlertTriangle, DollarSign, Minus, ArrowUpRight,
  CalendarClock
} from "lucide-react";

// --- Types ---
type CaseStatus = "open" | "in_progress" | "awaiting_payment" | "recovered" | "failed" | "escalated" | "closed";

interface PendingDecision {
  recommended_action: string;
  action_parameters?: Record<string, unknown>;
  confidence_score: number;
  reasoning: string;
}

interface PendingDiagnosis {
  category: string;
  specific_reason: string;
  confidence_score: number;
  reasoning: string;
}

interface RecoveryCase {
  id: string;
  customer_id: string;
  case_type: string;
  amount_paise: number;
  priority_score: number;
  status: CaseStatus;
  retry_count: number;
  cumulative_discount_paise: number;
  promise_to_pay_date: string | null;
  pending_decision_json: PendingDecision | null;
  pending_diagnosis_json: PendingDiagnosis | null;
  created_at: string;
}

interface ByTypeEntry {
  total: number;
  recovered: number;
  at_risk_paise: number;
  recovered_paise: number;
}

interface Analytics {
  total_cases: number;
  total_at_risk_paise: number;
  total_recovered_paise: number;
  total_discount_cost_paise: number;
  net_recovered_paise: number;
  recovery_rate_percent: number;
  net_recovery_rate_percent: number;
  breakdown_by_case_type: Record<string, ByTypeEntry>;
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

const API_BASE = "http://127.0.0.1:8000/api/v1";

const formatINR = (paise: number) =>
  (paise / 100).toLocaleString("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 0 });

const CASE_TYPE_LABELS: Record<string, string> = {
  subscription_failed: "Subscription Failed",
  checkout_abandoned: "Checkout Abandoned",
  invoice_overdue: "Invoice Overdue",
};

const STATUS_COLORS: Record<string, string> = {
  recovered: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20",
  in_progress: "bg-blue-500/10 text-blue-400 border-blue-500/20",
  failed: "bg-rose-500/10 text-rose-400 border-rose-500/20",
  escalated: "bg-amber-500/10 text-amber-400 border-amber-500/20",
  awaiting_payment: "bg-yellow-500/10 text-yellow-400 border-yellow-500/20",
  closed: "bg-slate-500/10 text-slate-400 border-slate-500/20",
  open: "bg-white/5 text-slate-300 border-white/10",
};

// Tomorrow as YYYY-MM-DD — must be at module level so component state can reference it
const tomorrowStr = () => {
  const d = new Date(); d.setDate(d.getDate() + 1);
  return d.toISOString().split("T")[0];
};

type Tab = "queue" | "escalation" | "analytics";

export default function Dashboard() {
  const [activeTab, setActiveTab] = useState<Tab>("queue");
  const [analytics, setAnalytics] = useState<Analytics | null>(null);
  const [cases, setCases] = useState<RecoveryCase[]>([]);
  const [escalatedCases, setEscalatedCases] = useState<RecoveryCase[]>([]);
  const [loading, setLoading] = useState(true);
  const [batchLoading, setBatchLoading] = useState(false);
  const [approvingId, setApprovingId] = useState<string | null>(null);

  // Promise-to-Pay state
  const [ptpModalOpen, setPtpModalOpen] = useState(false);
  const [ptpCaseId, setPtpCaseId] = useState<string | null>(null);
  const [ptpDate, setPtpDate] = useState(tomorrowStr());
  const [ptpNote, setPtpNote] = useState("");
  const [ptpLoading, setPtpLoading] = useState(false);
  const [ptpError, setPtpError] = useState<string | null>(null);

  const [auditModalOpen, setAuditModalOpen] = useState(false);
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null);
  const [auditLogs, setAuditLogs] = useState<AuditLog[]>([]);
  const [auditLoading, setAuditLoading] = useState(false);

  const loadData = useCallback(async () => {
    setLoading(true);
    try {
      const [analyticsRes, casesRes, escalatedRes] = await Promise.all([
        fetch(`${API_BASE}/analytics`),
        fetch(`${API_BASE}/cases?limit=100`),
        fetch(`${API_BASE}/cases/escalated`),
      ]);
      setAnalytics(await analyticsRes.json());
      setCases(await casesRes.json());
      setEscalatedCases(await escalatedRes.json());
    } catch (err) {
      console.error("Failed to fetch dashboard data:", err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 5000);
    return () => clearInterval(interval);
  }, [loadData]);

  const runBatch = async () => {
    setBatchLoading(true);
    try {
      await fetch(`${API_BASE}/batch`, { method: "POST" });
      await loadData();
    } catch (err) {
      console.error("Failed to run batch:", err);
    } finally {
      setBatchLoading(false);
    }
  };

  const viewAudit = async (caseId: string) => {
    setSelectedCaseId(caseId);
    setAuditModalOpen(true);
    setAuditLoading(true);
    try {
      const res = await fetch(`${API_BASE}/cases/${caseId}/audit`);
      setAuditLogs(await res.json());
    } catch (err) {
      console.error("Failed to fetch audit logs:", err);
    } finally {
      setAuditLoading(false);
    }
  };

  const approveCase = async (caseId: string) => {
    setApprovingId(caseId);
    try {
      await fetch(`${API_BASE}/cases/${caseId}/approve`, { method: "POST" });
      await loadData();
    } catch {
      alert("Failed to approve case");
    } finally {
      setApprovingId(null);
    }
  };

  const closeCase = async (caseId: string) => {
    setApprovingId(caseId);
    try {
      const res = await fetch(`${API_BASE}/cases/${caseId}/close`, { method: "POST" });
      if (!res.ok) throw new Error();
      await loadData();
    } catch {
      alert("Failed to close case");
    } finally {
      setApprovingId(null);
    }
  };

  const openPtpModal = (caseId: string) => {
    setPtpCaseId(caseId);
    setPtpDate(tomorrowStr());
    setPtpNote("");
    setPtpError(null);
    setPtpModalOpen(true);
  };

  const submitPtp = async () => {
    if (!ptpCaseId || !ptpDate) return;
    setPtpLoading(true);
    setPtpError(null);
    try {
      const res = await fetch(`${API_BASE}/cases/${ptpCaseId}/promise-to-pay`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ date: ptpDate, note: ptpNote || undefined }),
      });
      if (!res.ok) {
        const err = await res.json();
        setPtpError(err.detail ?? "Failed to capture promise.");
        return;
      }
      setPtpModalOpen(false);
      await loadData();
    } catch {
      setPtpError("Network error — please try again.");
    } finally {
      setPtpLoading(false);
    }
  };

  const tabs: { id: Tab; label: string; icon: React.ReactNode; count?: number }[] = [
    { id: "queue", label: "Live Queue", icon: <Inbox className="w-4 h-4" />, count: cases.length },
    { id: "escalation", label: "Escalation Queue", icon: <AlertTriangle className="w-4 h-4" />, count: escalatedCases.length },
    { id: "analytics", label: "Analytics", icon: <BarChart3 className="w-4 h-4" /> },
  ];

  return (
    <div className="min-h-screen bg-[#06080F] text-slate-200 font-sans selection:bg-indigo-500/30 overflow-hidden relative pb-10">
      {/* Background orbs */}
      <div className="fixed top-[-20%] left-[-10%] w-[50%] h-[50%] rounded-full bg-indigo-600/20 blur-[120px] pointer-events-none" />
      <div className="fixed bottom-[-20%] right-[-10%] w-[50%] h-[50%] rounded-full bg-emerald-600/10 blur-[120px] pointer-events-none" />
      <div className="fixed top-[20%] right-[20%] w-[30%] h-[30%] rounded-full bg-purple-600/10 blur-[100px] pointer-events-none" />

      <div className="relative max-w-7xl mx-auto space-y-6 p-8 z-10">

        {/* Header */}
        <div className="flex justify-between items-center bg-white/[0.03] p-5 rounded-2xl shadow-2xl border border-white/5 backdrop-blur-xl">
          <div className="flex items-center gap-4">
            <div className="p-3 bg-gradient-to-br from-indigo-500 to-purple-600 rounded-xl shadow-lg shadow-indigo-500/20">
              <Activity className="w-6 h-6 text-white" />
            </div>
            <div>
              <h1 className="text-2xl font-bold tracking-tight text-white flex items-center gap-2">
                Revenue Recovery Orchestrator
                <Badge variant="outline" className="bg-indigo-500/10 text-indigo-400 border-indigo-500/20 ml-2 text-xs">LIVE</Badge>
              </h1>
              <p className="text-slate-400 mt-0.5 text-sm">AI-orchestrated · Deterministic guardrails · Full audit trail</p>
            </div>
          </div>
          <div className="flex space-x-3">
            <Button variant="outline" onClick={loadData} disabled={loading}
              className="bg-white/5 border-white/10 text-slate-300 hover:bg-white/10 hover:text-white">
              <RefreshCw className={`w-4 h-4 mr-2 ${loading ? "animate-spin" : ""}`} />Refresh
            </Button>
            <Button onClick={runBatch} disabled={batchLoading}
              className="bg-indigo-600 hover:bg-indigo-500 text-white shadow-lg shadow-indigo-600/20 border border-indigo-500/50">
              <Play className={`w-4 h-4 mr-2 ${batchLoading ? "animate-pulse" : ""}`} />
              {batchLoading ? "Running…" : "Run 50+ Batch"}
            </Button>
          </div>
        </div>

        {/* Top KPI Strip */}
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {[
            { label: "Total Cases", value: analytics?.total_cases ?? "-", icon: <Users className="w-4 h-4" />, color: "text-white" },
            { label: "At Risk", value: analytics ? formatINR(analytics.total_at_risk_paise) : "-", icon: <AlertCircle className="w-4 h-4" />, color: "text-rose-400" },
            { label: "Gross Recovered", value: analytics ? formatINR(analytics.total_recovered_paise) : "-", icon: <ShieldCheck className="w-4 h-4" />, color: "text-emerald-400" },
            { label: "Recovery Rate", value: analytics ? `${analytics.recovery_rate_percent}%` : "-", icon: <TrendingUp className="w-4 h-4" />, color: "text-indigo-400" },
          ].map((kpi) => (
            <Card key={kpi.label} className="bg-white/[0.02] border-white/5 backdrop-blur-md shadow-xl hover:bg-white/[0.04] hover:border-white/10 transition-all group">
              <CardHeader className="pb-1 pt-4 px-4">
                <CardTitle className="text-xs font-semibold text-slate-400 uppercase tracking-widest flex items-center gap-2">
                  <span className="text-slate-500 group-hover:text-indigo-400 transition-colors">{kpi.icon}</span>
                  {kpi.label}
                </CardTitle>
              </CardHeader>
              <CardContent className="px-4 pb-4">
                <p className={`text-3xl font-bold tracking-tight ${kpi.color}`}>{kpi.value}</p>
              </CardContent>
            </Card>
          ))}
        </div>

        {/* Tab Bar */}
        <div className="flex gap-1 bg-white/[0.03] border border-white/5 rounded-xl p-1">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className={`flex items-center gap-2 px-4 py-2.5 rounded-lg text-sm font-medium transition-all flex-1 justify-center
                ${activeTab === tab.id
                  ? "bg-indigo-600/80 text-white shadow-md shadow-indigo-600/20 border border-indigo-500/50"
                  : "text-slate-400 hover:text-slate-200 hover:bg-white/5"}`}
            >
              {tab.icon}
              {tab.label}
              {tab.count !== undefined && (
                <span className={`ml-1 text-xs px-1.5 py-0.5 rounded-full font-mono
                  ${activeTab === tab.id ? "bg-white/20 text-white" : "bg-white/5 text-slate-500"}`}>
                  {tab.count}
                </span>
              )}
            </button>
          ))}
        </div>

        {/* ── LIVE QUEUE TAB ── */}
        {activeTab === "queue" && (
          <Card className="bg-white/[0.02] border-white/5 backdrop-blur-xl shadow-2xl overflow-hidden rounded-2xl">
            <CardHeader className="bg-white/[0.02] border-b border-white/5 px-6 py-4">
              <div className="flex justify-between items-center">
                <CardTitle className="text-base text-white font-medium">Recovery Cases — sorted by expected value</CardTitle>
                <Badge variant="outline" className="bg-white/5 border-white/10 text-slate-400 font-mono text-xs">
                  {cases.length} entries
                </Badge>
              </div>
            </CardHeader>
            <CardContent className="p-0">
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow className="hover:bg-transparent border-white/5 bg-black/20">
                      {["Case ID", "Customer", "Type", "Diagnosis", "Amount", "Exp. Recovery", "Status", "Attempts", "Discount", "Actions"].map((h) => (
                        <TableHead key={h} className="text-slate-400 font-semibold tracking-wider text-xs">{h}</TableHead>
                      ))}
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {cases.length === 0 && !loading && (
                      <TableRow className="border-white/5 hover:bg-transparent">
                        <TableCell colSpan={9} className="text-center py-16 text-slate-500">
                          <div className="flex flex-col items-center gap-3">
                            <AlertCircle className="w-8 h-8 opacity-50" />
                            <p>No cases found. Run a batch to ingest test cases.</p>
                          </div>
                        </TableCell>
                      </TableRow>
                    )}
                    {cases.map((c) => (
                      <TableRow key={c.id} className="border-white/5 hover:bg-white/[0.03] transition-colors group cursor-pointer" onClick={() => viewAudit(c.id)}>
                        <TableCell className="font-mono text-xs text-slate-500 group-hover:text-slate-300">{c.id.split("-")[0]}</TableCell>
                        <TableCell className="font-medium text-slate-200">{c.customer_id}</TableCell>
                        <TableCell className="text-slate-400 text-sm">{CASE_TYPE_LABELS[c.case_type] ?? c.case_type}</TableCell>
                        <TableCell className="text-slate-400 text-xs truncate max-w-[150px]" title={c.pending_diagnosis_json ? c.pending_diagnosis_json.reasoning : "Diagnosis runs on workflow execution"}>
                          {c.pending_diagnosis_json ? c.pending_diagnosis_json.category.replace(/_/g, " ") : "—"}
                        </TableCell>
                        <TableCell className="text-right font-medium text-slate-200">{formatINR(c.amount_paise)}</TableCell>
                        <TableCell className="text-right text-indigo-400 font-medium">{formatINR(c.priority_score)}</TableCell>
                        <TableCell>
                          <Badge variant="outline" className={`${STATUS_COLORS[c.status] ?? STATUS_COLORS.open} capitalize`}>
                            {c.status.replace(/_/g, " ")}
                          </Badge>
                        </TableCell>
                        <TableCell className="text-center text-slate-500 font-mono text-xs">{c.retry_count}</TableCell>
                        <TableCell className="text-right text-slate-500 text-xs">
                          {c.cumulative_discount_paise > 0 ? (
                            <span className="text-amber-400">{formatINR(c.cumulative_discount_paise)}</span>
                          ) : "—"}
                        </TableCell>
                        <TableCell>
                          <div className="flex items-center gap-1 justify-end">
                            {c.promise_to_pay_date && (
                              <Badge variant="outline" className="bg-teal-500/10 text-teal-400 border-teal-500/20 text-xs font-mono">
                                PTP {new Date(c.promise_to_pay_date + "T00:00:00").toLocaleDateString("en-IN", { day: "2-digit", month: "short" })}
                              </Badge>
                            )}
                            <Button variant="ghost" size="sm"
                              onClick={(e) => { e.stopPropagation(); openPtpModal(c.id); }}
                              title="Set Promise-to-Pay date"
                              className="text-teal-500/60 hover:text-teal-300 hover:bg-teal-500/10 rounded-lg">
                              <CalendarClock className="w-4 h-4" />
                            </Button>
                            <Button variant="ghost" size="sm"
                              onClick={(e) => { e.stopPropagation(); viewAudit(c.id); }}
                              className="text-slate-400 hover:text-indigo-300 hover:bg-indigo-500/10 rounded-lg">
                              <Search className="w-4 h-4" />
                            </Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            </CardContent>
          </Card>
        )}

        {/* ── ESCALATION QUEUE TAB ── */}
        {activeTab === "escalation" && (
          <div className="space-y-4">
            <div className="flex items-center gap-3 bg-amber-500/5 border border-amber-500/20 rounded-xl px-5 py-4">
              <AlertTriangle className="w-5 h-5 text-amber-400 shrink-0" />
              <p className="text-sm text-amber-200">
                These cases were blocked by policy (high value, low confidence, or manual flag) and require human review before re-processing.
                Click <strong>Approve</strong> to re-queue a case into the AI pipeline.
              </p>
            </div>

            {escalatedCases.length === 0 ? (
              <Card className="bg-white/[0.02] border-white/5 rounded-2xl">
                <CardContent className="py-20 text-center text-slate-500">
                  <CheckCircle2 className="w-10 h-10 mx-auto mb-3 opacity-40" />
                  <p>No cases in the escalation queue.</p>
                </CardContent>
              </Card>
            ) : (
              <div className="space-y-3">
                {escalatedCases.map((c) => (
                  <div key={c.id} className="bg-white/[0.02] border border-amber-500/15 rounded-xl px-5 py-4 flex items-center justify-between gap-4 hover:border-amber-500/30 transition-all">
                    <div className="flex items-start gap-4 flex-1 min-w-0">
                      <div className="p-2 bg-amber-500/10 rounded-lg shrink-0">
                        <AlertTriangle className="w-5 h-5 text-amber-400" />
                      </div>
                      <div className="min-w-0">
                        <div className="flex items-center gap-3 flex-wrap">
                          <span className="font-mono text-xs text-slate-500">{c.id.split("-")[0]}</span>
                          <span className="font-medium text-slate-200">{c.customer_id}</span>
                          <Badge variant="outline" className="bg-amber-500/10 text-amber-400 border-amber-500/20 text-xs">
                            {CASE_TYPE_LABELS[c.case_type] ?? c.case_type}
                          </Badge>
                        </div>
                        <div className="mt-1 flex items-center gap-4 text-sm text-slate-400 flex-wrap">
                          <span className="text-rose-400 font-semibold">{formatINR(c.amount_paise)}</span>
                          <span>· {c.retry_count} attempts</span>
                          {c.cumulative_discount_paise > 0 && (
                            <span className="text-amber-400">· {formatINR(c.cumulative_discount_paise)} discounted</span>
                          )}
                          <span>· created {new Date(c.created_at).toLocaleDateString("en-IN")}</span>
                        </div>
                      </div>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <Button variant="ghost" size="sm"
                        onClick={() => viewAudit(c.id)}
                        className="text-slate-400 hover:text-indigo-300 hover:bg-indigo-500/10 rounded-lg text-xs">
                        <Search className="w-3.5 h-3.5 mr-1.5" />View Audit
                      </Button>
                      
                      {c.pending_decision_json ? (
                        <div className="flex flex-col items-end gap-1">
                          <Button size="sm"
                            onClick={() => approveCase(c.id)}
                            disabled={approvingId === c.id}
                            className="bg-emerald-600/80 hover:bg-emerald-500 text-white border border-emerald-500/50 shadow-md shadow-emerald-600/20 rounded-lg text-xs">
                            {approvingId === c.id ? (
                              <RefreshCw className="w-3.5 h-3.5 mr-1.5 animate-spin" />
                            ) : (
                              <CheckCircle2 className="w-3.5 h-3.5 mr-1.5" />
                            )}
                            {approvingId === c.id ? "Approving…" : "Approve & Execute"}
                          </Button>
                          <span className="text-[10px] text-slate-500 max-w-[150px] text-right truncate">
                            Pending: {c.pending_decision_json.recommended_action.replace(/_/g, " ")}
                          </span>
                        </div>
                      ) : (
                        <Button size="sm" variant="outline"
                          onClick={() => closeCase(c.id)}
                          disabled={approvingId === c.id}
                          className="border-amber-500/30 text-amber-400 hover:bg-amber-500/10 rounded-lg text-xs">
                          {approvingId === c.id ? (
                            <RefreshCw className="w-3.5 h-3.5 mr-1.5 animate-spin" />
                          ) : (
                            <Search className="w-3.5 h-3.5 mr-1.5" />
                          )}
                          {approvingId === c.id ? "Closing…" : "Close Case"}
                        </Button>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* ── ANALYTICS TAB ── */}
        {activeTab === "analytics" && analytics && (
          <div className="space-y-6">

            {/* Net Economics Banner */}
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <Card className="bg-white/[0.02] border-white/5 hover:border-emerald-500/20 transition-all group">
                <CardHeader className="pb-1 pt-4 px-4">
                  <CardTitle className="text-xs font-semibold text-slate-400 uppercase tracking-widest flex items-center gap-2">
                    <DollarSign className="w-4 h-4 text-slate-500 group-hover:text-emerald-400 transition-colors" />Gross Recovered
                  </CardTitle>
                </CardHeader>
                <CardContent className="px-4 pb-4">
                  <p className="text-3xl font-bold text-emerald-400">{formatINR(analytics.total_recovered_paise)}</p>
                  <p className="text-xs text-slate-500 mt-1">{analytics.recovery_rate_percent}% recovery rate</p>
                </CardContent>
              </Card>
              <Card className="bg-white/[0.02] border-white/5 hover:border-amber-500/20 transition-all group">
                <CardHeader className="pb-1 pt-4 px-4">
                  <CardTitle className="text-xs font-semibold text-slate-400 uppercase tracking-widest flex items-center gap-2">
                    <Minus className="w-4 h-4 text-slate-500 group-hover:text-amber-400 transition-colors" />Discount Cost
                  </CardTitle>
                </CardHeader>
                <CardContent className="px-4 pb-4">
                  <p className="text-3xl font-bold text-amber-400">{formatINR(analytics.total_discount_cost_paise)}</p>
                  <p className="text-xs text-slate-500 mt-1">Cumulative across all cases</p>
                </CardContent>
              </Card>
              <Card className="bg-indigo-900/20 border-indigo-500/20 hover:border-indigo-500/40 transition-all group relative overflow-hidden">
                <div className="absolute inset-0 bg-gradient-to-br from-indigo-500/10 to-purple-500/5 opacity-0 group-hover:opacity-100 transition-opacity" />
                <CardHeader className="pb-1 pt-4 px-4 relative z-10">
                  <CardTitle className="text-xs font-semibold text-indigo-300 uppercase tracking-widest flex items-center gap-2">
                    <ArrowUpRight className="w-4 h-4" />Net Recovered (after discounts)
                  </CardTitle>
                </CardHeader>
                <CardContent className="px-4 pb-4 relative z-10">
                  <p className="text-3xl font-bold text-indigo-300">{formatINR(analytics.net_recovered_paise)}</p>
                  <p className="text-xs text-indigo-400 mt-1">{analytics.net_recovery_rate_percent}% net recovery rate</p>
                </CardContent>
              </Card>
            </div>

            {/* Breakdown by Case Type */}
            <Card className="bg-white/[0.02] border-white/5 rounded-2xl overflow-hidden">
              <CardHeader className="bg-white/[0.02] border-b border-white/5 px-6 py-4">
                <CardTitle className="text-base text-white font-medium">Breakdown by Case Type</CardTitle>
              </CardHeader>
              <CardContent className="p-0">
                <Table>
                  <TableHeader>
                    <TableRow className="hover:bg-transparent border-white/5 bg-black/20">
                      {["Case Type", "Total Cases", "Recovered", "Recovery Rate", "At Risk", "Recovered ₹"].map(h => (
                        <TableHead key={h} className="text-slate-400 font-semibold tracking-wider text-xs">{h}</TableHead>
                      ))}
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {Object.entries(analytics.breakdown_by_case_type).map(([type, data]) => {
                      const rate = data.total > 0 ? Math.round((data.recovered / data.total) * 100) : 0;
                      return (
                        <TableRow key={type} className="border-white/5 hover:bg-white/[0.03]">
                          <TableCell className="font-medium text-slate-200">{CASE_TYPE_LABELS[type] ?? type}</TableCell>
                          <TableCell className="text-slate-300 font-mono">{data.total}</TableCell>
                          <TableCell className="text-emerald-400 font-mono">{data.recovered}</TableCell>
                          <TableCell>
                            <div className="flex items-center gap-2">
                              <div className="flex-1 bg-white/5 rounded-full h-1.5 max-w-[80px]">
                                <div className="bg-emerald-500 h-1.5 rounded-full transition-all" style={{ width: `${rate}%` }} />
                              </div>
                              <span className="text-slate-300 text-xs font-mono w-8">{rate}%</span>
                            </div>
                          </TableCell>
                          <TableCell className="text-rose-400">{formatINR(data.at_risk_paise)}</TableCell>
                          <TableCell className="text-emerald-400">{formatINR(data.recovered_paise)}</TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </CardContent>
            </Card>

            {/* Status Breakdown + Exceptions */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              {/* Status Breakdown */}
              <Card className="bg-white/[0.02] border-white/5 rounded-2xl overflow-hidden">
                <CardHeader className="bg-white/[0.02] border-b border-white/5 px-5 py-4">
                  <CardTitle className="text-base text-white font-medium">Pipeline Status Distribution</CardTitle>
                </CardHeader>
                <CardContent className="p-5 space-y-3">
                  {Object.entries(analytics.breakdown_by_status)
                    .sort(([, a], [, b]) => b - a)
                    .map(([status, count]) => {
                      const pct = analytics.total_cases > 0 ? Math.round((count / analytics.total_cases) * 100) : 0;
                      return (
                        <div key={status} className="flex items-center gap-3">
                          <div className="w-28 shrink-0">
                            <Badge variant="outline" className={`${STATUS_COLORS[status] ?? STATUS_COLORS.open} capitalize text-xs`}>
                              {status.replace(/_/g, " ")}
                            </Badge>
                          </div>
                          <div className="flex-1 bg-white/5 rounded-full h-1.5">
                            <div className="bg-indigo-500 h-1.5 rounded-full transition-all" style={{ width: `${pct}%` }} />
                          </div>
                          <span className="text-slate-400 font-mono text-xs w-10 text-right">{count}</span>
                        </div>
                      );
                    })}
                </CardContent>
              </Card>

              {/* Exceptions */}
              <Card className="bg-white/[0.02] border-white/5 rounded-2xl overflow-hidden">
                <CardHeader className="bg-white/[0.02] border-b border-white/5 px-5 py-4">
                  <div className="flex justify-between items-center">
                    <CardTitle className="text-base text-white font-medium">Exception List</CardTitle>
                    <Badge variant="outline" className="bg-rose-500/10 text-rose-400 border-rose-500/20 text-xs font-mono">
                      {analytics.exceptions.length} cases
                    </Badge>
                  </div>
                </CardHeader>
                <CardContent className="p-0">
                  {analytics.exceptions.length === 0 ? (
                    <div className="py-10 text-center text-slate-500 text-sm">
                      <CheckCircle2 className="w-6 h-6 mx-auto mb-2 text-emerald-500 opacity-60" />
                      No exceptions — all cases resolved.
                    </div>
                  ) : (
                    <div className="max-h-64 overflow-y-auto divide-y divide-white/5">
                      {analytics.exceptions.map((ex) => (
                        <div key={ex.case_id} className="px-5 py-3 flex items-center justify-between hover:bg-white/[0.02]">
                          <div>
                            <div className="flex items-center gap-2">
                              <span className="font-mono text-xs text-slate-500">{ex.case_id.split("-")[0]}</span>
                              <Badge variant="outline" className={`${STATUS_COLORS[ex.status] ?? ""} capitalize text-xs`}>
                                {ex.status}
                              </Badge>
                            </div>
                            <p className="text-xs text-slate-400 mt-0.5">{CASE_TYPE_LABELS[ex.case_type] ?? ex.case_type}</p>
                          </div>
                          <span className="text-rose-400 font-semibold text-sm">{formatINR(ex.amount_paise)}</span>
                        </div>
                      ))}
                    </div>
                  )}
                </CardContent>
              </Card>
            </div>

            {/* Policy Config */}
            <PolicyConfigCard />
          </div>
        )}
      </div>

      {/* Promise-to-Pay Modal */}
      <Dialog open={ptpModalOpen} onOpenChange={setPtpModalOpen}>
        <DialogContent className="max-w-md bg-[#0F1523] border-white/10 shadow-2xl shadow-black p-0 gap-0">
          <DialogHeader className="p-6 pb-4 bg-white/[0.02] border-b border-white/5">
            <DialogTitle className="flex items-center text-lg text-white font-medium">
              <CalendarClock className="w-5 h-5 mr-3 text-teal-400" />
              Capture Promise-to-Pay
            </DialogTitle>
          </DialogHeader>
          <div className="p-6 space-y-5">
            <p className="text-sm text-slate-400">
              Record the date the customer committed to paying. Standard AI reminders will be suppressed until then.
              If payment is not received by this date, the case will auto-escalate.
            </p>
            <div>
              <label className="block text-xs text-slate-400 uppercase tracking-wider mb-2">Payment Date *</label>
              <input
                type="date"
                value={ptpDate}
                onChange={(e) => setPtpDate(e.target.value)}
                min={new Date().toISOString().split("T")[0]}
                className="w-full bg-white/5 border border-white/10 rounded-lg px-4 py-2.5 text-slate-200 text-sm focus:outline-none focus:border-teal-500/50 focus:ring-1 focus:ring-teal-500/30"
              />
            </div>
            <div>
              <label className="block text-xs text-slate-400 uppercase tracking-wider mb-2">Customer Note (optional)</label>
              <input
                type="text"
                value={ptpNote}
                onChange={(e) => setPtpNote(e.target.value)}
                placeholder="e.g. Customer said payment will clear on salary day"
                className="w-full bg-white/5 border border-white/10 rounded-lg px-4 py-2.5 text-slate-200 text-sm placeholder:text-slate-600 focus:outline-none focus:border-teal-500/50 focus:ring-1 focus:ring-teal-500/30"
              />
            </div>
            {ptpError && (
              <div className="bg-rose-500/10 border border-rose-500/20 rounded-lg px-4 py-3 text-rose-400 text-sm flex items-center gap-2">
                <AlertCircle className="w-4 h-4 shrink-0" />{ptpError}
              </div>
            )}
            <div className="flex gap-3 pt-1">
              <Button variant="outline" onClick={() => setPtpModalOpen(false)}
                className="flex-1 bg-white/5 border-white/10 text-slate-400 hover:text-white hover:bg-white/10">
                Cancel
              </Button>
              <Button onClick={submitPtp} disabled={ptpLoading || !ptpDate}
                className="flex-1 bg-teal-600/80 hover:bg-teal-500 text-white border border-teal-500/50">
                {ptpLoading ? <RefreshCw className="w-4 h-4 animate-spin mr-2" /> : <CalendarClock className="w-4 h-4 mr-2" />}
                {ptpLoading ? "Saving…" : "Capture Promise"}
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      {/* Audit Trail Modal */}

      <Dialog open={auditModalOpen} onOpenChange={setAuditModalOpen}>
        <DialogContent className="max-w-2xl max-h-[85vh] flex flex-col p-0 gap-0 overflow-hidden bg-[#0F1523] border-white/10 shadow-2xl shadow-black">
          <DialogHeader className="p-6 pb-4 bg-white/[0.02] border-b border-white/5">
            <DialogTitle className="flex items-center text-xl text-white font-medium tracking-tight">
              <Activity className="w-5 h-5 mr-3 text-indigo-400" />
              Decision & Audit Trace
              <span className="ml-4 text-xs font-mono text-slate-400 bg-black/40 px-3 py-1 rounded-full border border-white/5">
                {selectedCaseId}
              </span>
            </DialogTitle>
          </DialogHeader>
          <div className="p-6 overflow-y-auto flex-1 bg-gradient-to-b from-[#0F1523] to-[#0B0F19]">
            {auditLoading ? (
              <div className="flex justify-center items-center py-20">
                <RefreshCw className="w-8 h-8 animate-spin text-indigo-500" />
              </div>
            ) : auditLogs.length === 0 ? (
              <div className="flex flex-col items-center gap-3 py-16 text-slate-500">
                <AlertCircle className="w-8 h-8 opacity-50" />
                <p>No audit trail available for this case.</p>
              </div>
            ) : (
              <div className="relative border-l border-indigo-500/30 ml-4 space-y-8 pb-8 pt-4">
                {auditLogs.map((log) => (
                  <div key={log.id} className="relative pl-8 group">
                    <div className="absolute w-4 h-4 bg-[#0F1523] border-2 border-indigo-500 rounded-full -left-[9px] top-1 shadow-[0_0_10px_rgba(99,102,241,0.5)] group-hover:scale-125 transition-transform duration-300" />
                    <div className="bg-white/[0.03] rounded-xl p-5 border border-white/5 shadow-lg group-hover:border-indigo-500/30 group-hover:bg-white/[0.05] transition-all duration-300">
                      <div className="flex justify-between items-start mb-3">
                        <span className="font-semibold text-xs text-indigo-300 bg-indigo-500/10 px-2.5 py-1 rounded-md border border-indigo-500/20 tracking-wider uppercase">
                          {log.action_type.replace(/_/g, " ")}
                        </span>
                        <span className="text-xs text-slate-500 font-mono bg-black/20 px-2 py-1 rounded border border-white/5">
                          {new Date(log.created_at).toLocaleTimeString([], { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" })}
                        </span>
                      </div>
                      <p className="text-slate-300 text-sm leading-relaxed">{log.description}</p>
                      {log.reasoning && (
                        <div className="mt-4 bg-black/40 border-l-2 border-indigo-500 p-4 rounded-r-lg text-sm">
                          <div className="flex gap-3">
                            <ChevronRight className="w-4 h-4 text-indigo-500 shrink-0 mt-0.5" />
                            <p className="text-slate-400 italic font-light">&quot;{log.reasoning}&quot;</p>
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function PolicyConfigCard() {
  const [policy, setPolicy] = useState<{ max_retries: number; max_discount_percent: number; require_human_approval_above_paise: number; block_hard_declines: boolean } | null>(null);

  useEffect(() => {
    fetch(`${API_BASE}/policy`).then(r => r.json()).then(setPolicy).catch(console.error);
  }, []);

  if (!policy) return null;

  return (
    <Card className="bg-white/[0.02] border-white/5 rounded-2xl">
      <CardHeader className="bg-white/[0.02] border-b border-white/5 px-6 py-4">
        <CardTitle className="text-base text-white font-medium flex items-center gap-2">
          <ShieldCheck className="w-5 h-5 text-indigo-400" />
          Live Policy Configuration
        </CardTitle>
      </CardHeader>
      <CardContent className="p-6">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {[
            { label: "Max Retries", value: policy.max_retries },
            { label: "Max Discount", value: `${policy.max_discount_percent}%` },
            { label: "Human Approval Above", value: formatINR(policy.require_human_approval_above_paise) },
            { label: "Block Hard Declines", value: policy.block_hard_declines ? "Yes" : "No" },
          ].map((item) => (
            <div key={item.label} className="bg-white/[0.03] rounded-xl p-4 border border-white/5">
              <p className="text-xs text-slate-500 uppercase tracking-wider mb-2">{item.label}</p>
              <p className="text-lg font-bold text-indigo-300">{item.value}</p>
            </div>
          ))}
        </div>
        <p className="text-xs text-slate-500 mt-4">
          + RBI eNACH 72h pre-debit notice · Max 30-day pursuit window · Customer opt-out stops recovery
        </p>
      </CardContent>
    </Card>
  );
}
