"use client";

import { useEffect, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { RefreshCw, Play, Search, AlertCircle, Activity, ShieldCheck, TrendingUp, ChevronRight } from "lucide-react";

// --- Types ---
type CaseStatus = "open" | "in_progress" | "awaiting_payment" | "recovered" | "failed" | "escalated" | "closed";

interface RecoveryCase {
  id: string;
  customer_id: string;
  case_type: string;
  amount_paise: number;
  priority_score: number;
  status: CaseStatus;
  retry_count: number;
}

interface Analytics {
  total_cases: number;
  total_at_risk_paise: number;
  total_recovered_paise: number;
  recovery_rate_percent: number;
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

export default function Dashboard() {
  const [analytics, setAnalytics] = useState<Analytics | null>(null);
  const [cases, setCases] = useState<RecoveryCase[]>([]);
  const [loading, setLoading] = useState(true);
  const [batchLoading, setBatchLoading] = useState(false);
  
  const [auditModalOpen, setAuditModalOpen] = useState(false);
  const [selectedCaseId, setSelectedCaseId] = useState<string | null>(null);
  const [auditLogs, setAuditLogs] = useState<AuditLog[]>([]);
  const [auditLoading, setAuditLoading] = useState(false);

  const loadData = async () => {
    setLoading(true);
    try {
      const [analyticsRes, casesRes] = await Promise.all([
        fetch(`${API_BASE}/analytics`),
        fetch(`${API_BASE}/cases?limit=100`)
      ]);
      const analyticsData = await analyticsRes.json();
      const casesData = await casesRes.json();
      setAnalytics(analyticsData);
      setCases(casesData);
    } catch (err) {
      console.error("Failed to fetch dashboard data:", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
    const interval = setInterval(loadData, 3000);
    return () => clearInterval(interval);
  }, []);

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
      const data = await res.json();
      setAuditLogs(data);
    } catch (err) {
      console.error("Failed to fetch audit logs:", err);
    } finally {
      setAuditLoading(false);
    }
  };

  const getStatusColor = (status: CaseStatus) => {
    switch (status) {
      case "recovered": return "bg-emerald-500/10 text-emerald-400 border-emerald-500/20";
      case "in_progress": return "bg-blue-500/10 text-blue-400 border-blue-500/20";
      case "failed": return "bg-rose-500/10 text-rose-400 border-rose-500/20";
      case "escalated": return "bg-amber-500/10 text-amber-400 border-amber-500/20";
      case "awaiting_payment": return "bg-yellow-500/10 text-yellow-400 border-yellow-500/20";
      case "closed": return "bg-slate-500/10 text-slate-400 border-slate-500/20";
      default: return "bg-white/5 text-slate-300 border-white/10";
    }
  };

  return (
    <div className="min-h-screen bg-[#06080F] text-slate-200 font-sans selection:bg-indigo-500/30 overflow-hidden relative pb-10">
      {/* Background Glowing Orbs */}
      <div className="fixed top-[-20%] left-[-10%] w-[50%] h-[50%] rounded-full bg-indigo-600/20 blur-[120px] pointer-events-none" />
      <div className="fixed bottom-[-20%] right-[-10%] w-[50%] h-[50%] rounded-full bg-emerald-600/10 blur-[120px] pointer-events-none" />
      <div className="fixed top-[20%] right-[20%] w-[30%] h-[30%] rounded-full bg-purple-600/10 blur-[100px] pointer-events-none" />

      <div className="relative max-w-7xl mx-auto space-y-8 p-8 z-10">
        
        {/* Header */}
        <div className="flex justify-between items-center bg-white/[0.03] p-6 rounded-2xl shadow-2xl border border-white/5 backdrop-blur-xl transition-all duration-300 hover:border-white/10">
          <div className="flex items-center gap-4">
            <div className="p-3 bg-gradient-to-br from-indigo-500 to-purple-600 rounded-xl shadow-lg shadow-indigo-500/20">
              <Activity className="w-6 h-6 text-white" />
            </div>
            <div>
              <h1 className="text-3xl font-bold tracking-tight text-white flex items-center gap-2">
                Revenue Recovery
                <Badge variant="outline" className="bg-indigo-500/10 text-indigo-400 border-indigo-500/20 ml-2">LIVE</Badge>
              </h1>
              <p className="text-slate-400 mt-1 text-sm font-medium">AI-orchestrated recovery with deterministic guardrails.</p>
            </div>
          </div>
          <div className="flex space-x-4">
            <Button 
              variant="outline" 
              onClick={loadData} 
              disabled={loading}
              className="bg-white/5 border-white/10 text-slate-300 hover:bg-white/10 hover:text-white transition-all duration-200"
            >
              <RefreshCw className={`w-4 h-4 mr-2 ${loading ? "animate-spin" : ""}`} />
              Refresh
            </Button>
            <Button 
              onClick={runBatch} 
              disabled={batchLoading} 
              className="bg-indigo-600 hover:bg-indigo-500 text-white shadow-lg shadow-indigo-600/20 transition-all duration-200 border border-indigo-500/50"
            >
              <Play className={`w-4 h-4 mr-2 ${batchLoading ? "animate-pulse" : ""}`} />
              Run 50+ Batch
            </Button>
          </div>
        </div>

        {/* Analytics Cards */}
        <div className="grid grid-cols-1 md:grid-cols-4 gap-6">
          <Card className="bg-white/[0.02] border-white/5 backdrop-blur-md shadow-xl hover:bg-white/[0.04] hover:border-white/10 transition-all duration-300 group">
            <CardHeader className="pb-2">
              <CardTitle className="text-xs font-semibold text-slate-400 uppercase tracking-widest flex items-center gap-2">
                <Activity className="w-4 h-4 text-slate-500 group-hover:text-indigo-400 transition-colors" />
                Total Cases
              </CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-4xl font-bold text-white tracking-tight">{analytics?.total_cases ?? "-"}</p>
            </CardContent>
          </Card>
          
          <Card className="bg-white/[0.02] border-white/5 backdrop-blur-md shadow-xl hover:bg-white/[0.04] hover:border-white/10 transition-all duration-300 group">
            <CardHeader className="pb-2">
              <CardTitle className="text-xs font-semibold text-slate-400 uppercase tracking-widest flex items-center gap-2">
                <AlertCircle className="w-4 h-4 text-slate-500 group-hover:text-rose-400 transition-colors" />
                Amount At Risk
              </CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-4xl font-bold text-rose-400 tracking-tight drop-shadow-sm">
                {analytics ? formatINR(analytics.total_at_risk_paise) : "-"}
              </p>
            </CardContent>
          </Card>
          
          <Card className="bg-white/[0.02] border-white/5 backdrop-blur-md shadow-xl hover:bg-white/[0.04] hover:border-white/10 transition-all duration-300 group">
            <CardHeader className="pb-2">
              <CardTitle className="text-xs font-semibold text-slate-400 uppercase tracking-widest flex items-center gap-2">
                <ShieldCheck className="w-4 h-4 text-slate-500 group-hover:text-emerald-400 transition-colors" />
                Recovered
              </CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-4xl font-bold text-emerald-400 tracking-tight drop-shadow-sm">
                {analytics ? formatINR(analytics.total_recovered_paise) : "-"}
              </p>
            </CardContent>
          </Card>
          
          <Card className="bg-white/[0.02] border-white/5 backdrop-blur-md shadow-xl hover:bg-white/[0.04] hover:border-white/10 transition-all duration-300 group relative overflow-hidden">
            <div className="absolute inset-0 bg-gradient-to-br from-indigo-500/10 to-purple-500/10 opacity-0 group-hover:opacity-100 transition-opacity duration-500" />
            <CardHeader className="pb-2 relative z-10">
              <CardTitle className="text-xs font-semibold text-slate-400 uppercase tracking-widest flex items-center gap-2">
                <TrendingUp className="w-4 h-4 text-slate-500 group-hover:text-indigo-400 transition-colors" />
                Recovery Rate
              </CardTitle>
            </CardHeader>
            <CardContent className="relative z-10">
              <p className="text-4xl font-bold text-indigo-400 tracking-tight drop-shadow-sm">
                {analytics ? `${analytics.recovery_rate_percent}%` : "-"}
              </p>
            </CardContent>
          </Card>
        </div>

        {/* Cases Table */}
        <Card className="bg-white/[0.02] border-white/5 backdrop-blur-xl shadow-2xl overflow-hidden rounded-2xl">
          <CardHeader className="bg-white/[0.02] border-b border-white/5 px-6 py-5">
            <div className="flex justify-between items-center">
              <CardTitle className="text-lg text-white font-medium">Recent Activity Log</CardTitle>
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
                    <TableHead className="w-[120px] text-slate-400 font-semibold tracking-wider text-xs">CASE ID</TableHead>
                    <TableHead className="text-slate-400 font-semibold tracking-wider text-xs">CUSTOMER</TableHead>
                    <TableHead className="text-slate-400 font-semibold tracking-wider text-xs">TYPE</TableHead>
                    <TableHead className="text-right text-slate-400 font-semibold tracking-wider text-xs">AMOUNT</TableHead>
                    <TableHead className="text-right text-indigo-400 font-semibold tracking-wider text-xs">EXPECTED RECOVERY</TableHead>
                    <TableHead className="text-center text-slate-400 font-semibold tracking-wider text-xs">STATUS</TableHead>
                    <TableHead className="text-center text-slate-400 font-semibold tracking-wider text-xs">ATTEMPTS</TableHead>
                    <TableHead className="text-right text-slate-400 font-semibold tracking-wider text-xs">ACTION</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {cases.length === 0 && !loading && (
                    <TableRow className="border-white/5 hover:bg-transparent">
                      <TableCell colSpan={8} className="text-center py-16 text-slate-500">
                        <div className="flex flex-col items-center gap-3">
                          <AlertCircle className="w-8 h-8 opacity-50" />
                          <p>No cases found. Run a batch to ingest test cases.</p>
                        </div>
                      </TableCell>
                    </TableRow>
                  )}
                  {cases.map((c) => (
                    <TableRow key={c.id} className="border-white/5 hover:bg-white/[0.03] transition-colors group cursor-pointer" onClick={() => viewAudit(c.id)}>
                      <TableCell className="font-mono text-xs text-slate-500 group-hover:text-slate-300 transition-colors">
                        {c.id.split('-')[0]}
                      </TableCell>
                      <TableCell className="font-medium text-slate-200">{c.customer_id}</TableCell>
                      <TableCell className="text-slate-400 capitalize text-sm">
                        {c.case_type.replace(/_/g, " ")}
                      </TableCell>
                      <TableCell className="text-right font-medium text-slate-200">
                        {formatINR(c.amount_paise)}
                      </TableCell>
                      <TableCell className="text-right text-indigo-400 font-medium bg-indigo-500/[0.02]">
                        {/* Display priority score converted to INR */}
                        {formatINR(c.priority_score)}
                      </TableCell>
                      <TableCell className="text-center">
                        <Badge variant="outline" className={`${getStatusColor(c.status)} capitalize shadow-sm`}>
                          {c.status.replace(/_/g, " ")}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-center text-slate-500 font-mono text-xs">
                        {c.retry_count}
                      </TableCell>
                      <TableCell className="text-right">
                        <Button 
                          variant="ghost" 
                          size="sm"
                          onClick={(e) => { e.stopPropagation(); viewAudit(c.id); }}
                          className="text-slate-400 hover:text-indigo-300 hover:bg-indigo-500/10 transition-all rounded-lg group/btn"
                        >
                          <Search className="w-4 h-4 group-hover/btn:scale-110 transition-transform" />
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Audit Modal */}
      <Dialog open={auditModalOpen} onOpenChange={setAuditModalOpen}>
        <DialogContent className="max-w-2xl max-h-[85vh] flex flex-col p-0 gap-0 overflow-hidden bg-[#0F1523] border-white/10 shadow-2xl shadow-black">
          <DialogHeader className="p-6 pb-4 bg-white/[0.02] border-b border-white/5 backdrop-blur-md">
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
                    {/* Glowing Timeline dot */}
                    <div className="absolute w-4 h-4 bg-[#0F1523] border-2 border-indigo-500 rounded-full -left-[9px] top-1 shadow-[0_0_10px_rgba(99,102,241,0.5)] group-hover:scale-125 transition-transform duration-300"></div>
                    
                    <div className="bg-white/[0.03] rounded-xl p-5 border border-white/5 shadow-lg group-hover:border-indigo-500/30 group-hover:bg-white/[0.05] transition-all duration-300">
                      <div className="flex justify-between items-start mb-3">
                        <span className="font-semibold text-xs text-indigo-300 bg-indigo-500/10 px-2.5 py-1 rounded-md border border-indigo-500/20 tracking-wider uppercase">
                          {log.action_type.replace(/_/g, " ")}
                        </span>
                        <span className="text-xs text-slate-500 font-mono bg-black/20 px-2 py-1 rounded border border-white/5">
                          {new Date(log.created_at).toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second:'2-digit' })}
                        </span>
                      </div>
                      <p className="text-slate-300 text-sm leading-relaxed">{log.description}</p>
                      
                      {log.reasoning && (
                        <div className="mt-4 bg-black/40 border-l-2 border-indigo-500 p-4 rounded-r-lg text-sm relative overflow-hidden group-hover:bg-black/60 transition-colors">
                          <div className="absolute top-0 right-0 p-2 opacity-5">
                            <AlertCircle className="w-16 h-16" />
                          </div>
                          <div className="flex gap-3 relative z-10">
                            <ChevronRight className="w-4 h-4 text-indigo-500 shrink-0 mt-0.5" />
                            <p className="text-slate-400 italic font-light">
                              &quot;{log.reasoning}&quot;
                            </p>
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
