export type SymbolName = "BTC" | "ETH" | "SOL";
export interface Quote {
  symbol: SymbolName; price: number | null; change: number | null; as_of: string; source: string; unit: string;
  volume: number | null; funding: number | null; interest: number | null; derivatives_as_of: string | null;
  spark: [string, number][]; origin: "collector" | "report";
}
export interface Issue {
  id: string; kind: string; date: string; title: string; summary: string; published_at: string;
  as_of: string; until: string; current: boolean; url: string;
}
export interface Edition {
  market: Quote[]; headline: Issue | null; evidence: {id:string;statement:string}[];
  products:{daily:Issue|null;standard:Issue|null;insight:Issue|null};
  recent:{id:string;title:string;report_type:string;published_at:string;created_at:string}[];
  notice:{message:string}|null;
}
export type PerformanceData = {
  times: number[]; series: {symbol:SymbolName; values:(number|null)[]}[]; step:number;
};
