/** Browser contracts exposed by the existing terminal and article entry points. */
interface DeskRow {
  symbol:string; current_view:string; previous_view?:string; action:string; funding_rate?:number;
}
interface DeskReport {
  id:string; published_at?:string; created_at:string;
  tactical_monitor?:{next_checkpoint:string; dashboard:DeskRow[]};
  facts?:{id:string;statement:string;category:string;as_of:string;window:string;source:string;url:string}[];
}
declare const liveReport:DeskReport|null,selected:string,selectedGranularity:number;
declare const assets:Record<string,{stance:string}>;
declare const visibleCandles:{time:number;open:number;close:number;volume:number}[];
declare function renderLiveChart():void;
declare function fmt(value:string):string;
