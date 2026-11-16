# 专业数据网关协议

设置 `MERIDIAN_PRO_DATA_URL` 后，系统通过 HTTP GET 获取下列 JSON。可在网关前连接 Glassnode、CryptoQuant、CoinGlass、CME 授权数据、ETF ETL 或内部数据库。

```json
{
  "assets": [
    {
      "symbol": "BTC",
      "as_of": "2026-07-11T16:00:00+08:00",
      "spot_etf_net_flow_usd": 500000000,
      "exchange_netflow_usd": -120000000,
      "stablecoin_supply_change_7d_pct": 1.4,
      "options_25d_skew_pct": 2.1,
      "cme_basis_annualized_pct": 8.5,
      "active_addresses_24h": 950000,
      "transaction_count_24h": 620000,
      "sources": ["licensed-etf-feed", "licensed-onchain-feed"]
    }
  ],
  "market_context": {
    "liquidations": {
      "as_of": "2026-07-11T16:00:00+08:00",
      "coverage": "MARKET_WIDE",
      "window": "24H",
      "total_usd_24h": 420000000,
      "long_usd_24h": 260000000,
      "short_usd_24h": 160000000,
      "venues": ["licensed-aggregate"],
      "source": "licensed-liquidation-feed",
      "note": "按供应商口径覆盖全市场"
    }
  }
}
```

## 符号口径

- `spot_etf_net_flow_usd`：正值为 ETF 净申购，负值为净赎回。
- `exchange_netflow_usd`：正值为流入交易所，负值为流出交易所。
- `stablecoin_supply_change_7d_pct`：相关链或市场的稳定币供给七日百分比变化。
- `options_25d_skew_pct`：Put IV 减 Call IV；正值代表下行保护溢价更高。
- `cme_basis_annualized_pct`：CME 期货相对可信现货指数的年化基差。
- `active_addresses_24h` / `transaction_count_24h`：供应商已去机器人/女巫噪声的 24H 链上活跃口径；未去噪时必须在 `sources` 或网关元数据说明。
- `liquidations.coverage`：只能是 `MARKET_WIDE`、`VENUE_SAMPLE` 或 `UNAVAILABLE`；交易所样本禁止标记为全市场。
- `sources`：必须填写供应商或内部数据集名称，禁止写 `unknown`。

通过 `MERIDIAN_PRO_DATA_TOKEN` 配置 Bearer Token。网关失败时固定报告仍会发布，但报告会显示专业资金数据降级，不会把缺失字段当作零。
