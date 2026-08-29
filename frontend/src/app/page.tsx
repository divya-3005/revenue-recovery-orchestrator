"use client";

import { useEffect, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { RefreshCw, Play, Search, AlertCircle } from "lucide-react";

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
  (paise / 100).toLocaleString("en-IN", { style: "currency", currency: "INR" });

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
      
      // Backend returns cases sorted by priority_score descending, then created_at descending.
      setCases(casesData);
    } catch (err) {
      console.error("Failed to fetch dashboard data:", err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadData();
    // Setup basic polling to auto-refresh (as cases process in background)
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
      case "recovered": return "bg-green-100 text-green-800 border-green-200";
      case "in_progress": return "bg-blue-100 text-blue-800 border-blue-200";
      case "failed": return "bg-red-100 text-red-800 border-red-200";
      case "escalated": return "bg-orange-100 text-orange-800 border-orange-200";
      case "awaiting_payment": return "bg-yellow-100 text-yellow-800 border-yellow-200";
      case "closed": return "bg-gray-200 text-gray-700 border-gray-300";
      default: return "bg-gray-100 text-gray-800 border-gray-200";
    }
  };

  return (
    <div className="min-h-screen bg-slate-50 p-8 font-sans text-slate-900">
      <div className="max-w-7xl mx-auto space-y-8">
        
        {/* Header */}
        <div className="flex justify-between items-center bg-white p-6 rounded-xl shadow-sm border border-slate-200">
          <div>
            <h1 className="text-3xl font-bold tracking-tight text-slate-900">Revenue Recovery</h1>
            <p className="text-slate-500 mt-1">AI-orchestrated recovery with deterministic policy boundaries.</p>
          </div>
          <div className="flex space-x-3">
            <Button variant="outline" onClick={loadData} disabled={loading}>
              <RefreshCw className={`w-4 h-4 mr-2 ${loading ? "animate-spin" : ""}`} />
              Refresh
            </Button>
            <Button onClick={runBatch} disabled={batchLoading} className="bg-indigo-600 hover:bg-indigo-700 text-white">
              <Play className={`w-4 h-4 mr-2 ${batchLoading ? "animate-pulse" : ""}`} />
              Run 50+ Batch
            </Button>
          </div>
        </div>

        {/* Analytics Cards */}
        <div className="grid grid-cols-1 md:grid-cols-4 gap-6">
          <Card className="border-slate-200 shadow-sm">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-slate-500 uppercase tracking-wider">Total Cases</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-3xl font-bold">{analytics?.total_cases ?? "-"}</p>
            </CardContent>
          </Card>
          <Card className="border-slate-200 shadow-sm">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-slate-500 uppercase tracking-wider">Amount At Risk</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-3xl font-bold text-red-600">
                {analytics ? formatINR(analytics.total_at_risk_paise) : "-"}
              </p>
            </CardContent>
          </Card>
          <Card className="border-slate-200 shadow-sm">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-slate-500 uppercase tracking-wider">Recovered</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-3xl font-bold text-emerald-600">
                {analytics ? formatINR(analytics.total_recovered_paise) : "-"}
              </p>
            </CardContent>
          </Card>
          <Card className="border-slate-200 shadow-sm">
            <CardHeader className="pb-2">
              <CardTitle className="text-sm font-medium text-slate-500 uppercase tracking-wider">Recovery Rate</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-3xl font-bold text-indigo-600">
                {analytics ? `${analytics.recovery_rate_percent}%` : "-"}
              </p>
            </CardContent>
          </Card>
        </div>

        {/* Cases Table */}
        <Card className="border-slate-200 shadow-sm overflow-hidden">
          <CardHeader className="bg-slate-50/50 border-b border-slate-200">
            <CardTitle>Recent Activity</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow className="hover:bg-transparent">
                    <TableHead className="w-[120px]">Case ID</TableHead>
                    <TableHead>Customer</TableHead>
                    <TableHead>Type</TableHead>
                    <TableHead className="text-right">Amount</TableHead>
                    <TableHead className="text-right text-indigo-600">Priority</TableHead>
                    <TableHead className="text-center">Status</TableHead>
                    <TableHead className="text-center">Attempts</TableHead>
                    <TableHead className="text-right">Action</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {cases.length === 0 && !loading && (
                    <TableRow>
                      <TableCell colSpan={8} className="text-center py-10 text-slate-500">
                        No cases found. Run a batch to ingest test cases.
                      </TableCell>
                    </TableRow>
                  )}
                  {cases.map((c) => (
                    <TableRow key={c.id} className="hover:bg-slate-50 transition-colors group">
                      <TableCell className="font-mono text-xs text-slate-500">
                        {c.id.split('-')[0]}
                      </TableCell>
                      <TableCell className="font-medium text-slate-900">{c.customer_id}</TableCell>
                      <TableCell className="text-slate-600 capitalize">
                        {c.case_type.replace(/_/g, " ")}
                      </TableCell>
                      <TableCell className="text-right font-medium text-slate-900">
                        {formatINR(c.amount_paise)}
                      </TableCell>
                      <TableCell className="text-right text-indigo-600 font-medium">
                        {c.priority_score.toLocaleString()}
                      </TableCell>
                      <TableCell className="text-center">
                        <Badge variant="outline" className={`${getStatusColor(c.status)} capitalize`}>
                          {c.status.replace(/_/g, " ")}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-center text-slate-500 text-sm">
                        {c.retry_count}
                      </TableCell>
                      <TableCell className="text-right">
                        <Button 
                          variant="ghost" 
                          size="sm"
                          onClick={() => viewAudit(c.id)}
                          className="text-indigo-600 hover:text-indigo-700 hover:bg-indigo-50"
                        >
                          <Search className="w-4 h-4 mr-2" />
                          View Trace
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
        <DialogContent className="max-w-2xl max-h-[85vh] flex flex-col p-0 gap-0 overflow-hidden bg-slate-50">
          <DialogHeader className="p-6 pb-4 bg-white border-b border-slate-200">
            <DialogTitle className="flex items-center text-xl">
              <AlertCircle className="w-5 h-5 mr-2 text-indigo-500" />
              Decision & Audit Trace
              <span className="ml-3 text-xs font-mono font-normal text-slate-400 bg-slate-100 px-2 py-1 rounded">
                {selectedCaseId}
              </span>
            </DialogTitle>
          </DialogHeader>
          <div className="p-6 overflow-y-auto flex-1">
            {auditLoading ? (
              <div className="flex justify-center items-center py-12">
                <RefreshCw className="w-6 h-6 animate-spin text-indigo-400" />
              </div>
            ) : auditLogs.length === 0 ? (
              <p className="text-center text-slate-500 py-10">No audit trail available.</p>
            ) : (
              <div className="relative border-l-2 border-indigo-200 ml-3 space-y-6 pb-4">
                {auditLogs.map((log) => (
                  <div key={log.id} className="relative pl-6">
                    {/* Timeline dot */}
                    <div className="absolute w-3 h-3 bg-white border-2 border-indigo-500 rounded-full -left-[7px] top-1.5 shadow-sm"></div>
                    
                    <div className="bg-white rounded-lg p-4 border border-slate-200 shadow-sm">
                      <div className="flex justify-between items-start mb-2">
                        <span className="font-semibold text-sm text-indigo-900 bg-indigo-50 px-2 py-0.5 rounded">
                          {log.action_type}
                        </span>
                        <span className="text-xs text-slate-400 font-mono">
                          {new Date(log.created_at).toLocaleTimeString()}
                        </span>
                      </div>
                      <p className="text-slate-700 text-sm">{log.description}</p>
                      
                      {log.reasoning && (
                        <div className="mt-3 bg-slate-50 border-l-2 border-indigo-300 p-3 rounded-r-md text-sm">
                          <p className="text-slate-600 italic">
                            &quot;{log.reasoning}&quot;
                          </p>
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
