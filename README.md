# FlyMerge

ハエ脳でPRを裁くジョーク機械です。unified diffを小さな刺激語彙へ変換し、実在するFlyBrain由来のconnectomeをCSR/LIFでシミュレーションして `approve` / `reject` / `hold` を出します。これはソフトウェア品質を保証する仕組みではありません。

## すぐ試す

固定実dataはこのrepositoryの `data/connectome.bin.gz` に含めています。

```powershell
python flymerge.py --self-test
@'
diff --git a/docs/demo.md b/docs/demo.md
--- a/docs/demo.md
+++ b/docs/demo.md
@@ -1 +1,2 @@
 # Demo
+A harmless documentation example.
'@ | Set-Content demo.diff
python flymerge.py --diff demo.diff --ticks 20 --gain 16 --compare-no-edges --output demo.json --markdown demo.md
```

`--compare-no-edges` は同じ刺激を実データの接続あり/なしで再実行し、edgeを通ったシナプス入力イベント数・入力総量と発火数の差をJSONへ保存します。`connectome.bin.gz` がない場合は実行を中止し、合成・乱数データへフォールバックしません。

## 固定データと出典

実装とデータの再現元は [snedea/flybrain](https://github.com/snedea/flybrain) の固定commit `9191824d17871b7851645782d53d23f213ddb938` です。配布する最小binaryのSHA-256は次のとおりです。

| file | SHA-256 |
| --- | --- |
| `data/connectome.bin.gz` | `FBF8D440CA1207C7573E1ACDD2366F9D0BEB9B533C1710F21681264F81B1CC49` |

固定binaryの実測ヘッダは `139,255` neurons / `2,698,236` directed edgesです。FlyWire public release dataの利用条件はCC BY-NC 4.0です。再配布・商用利用は [FlyWire citing guidelines](https://flywire.ai/guidelines) と最新の利用規約に従ってください。主論文は [Dorkenwald et al., Nature 634, 124–138 (2024)](https://doi.org/10.1038/s41586-024-07558-y) です。

## GitHub Actions

`.github/workflows/flymerge.yml` はこのrepositoryのdefault branchにあるtrusted実装とdataだけを実行します。

```text
.
├── data/connectome.bin.gz
├── flymerge.py
└── .github/workflows/flymerge.yml
```

`pull_request_target` と手動実行のどちらでも、最初にGitHub APIからPRの `base.sha` / `head.sha` とdefault branchの現在SHAを取得します。そのdefault branch SHAだけをcheckoutし、PR headはcheckoutせず、PR差分は `GET /repos/{owner}/{repo}/pulls/{number}` に `Accept: application/vnd.github.diff` を付けて取得します。取得前後のmetadataで `base.sha` / `head.sha` が変わっていないことも確認します。

merge jobは手動実行で `merge=true` かつjudgeの `approve` の場合だけ有効です。trusted実装を再度固定SHAから実行し、現在APIから取得したdiffと `head.sha` をCLIへ渡します。CLI自身もAPIからPR metadata/diffを再取得して、評価済みdiffのSHA-256とhead SHAが一致しない場合はmerge endpointへ到達しません。`hold` / `reject`、head変更、diff変更、`merged:false` はすべてmerge失敗またはjob skipになります。tokenはargvへ出さず `GITHUB_TOKEN` 環境変数だけで渡します。

Actionsを手動実行する場合は、対象PR番号を入力し、judge結果を確認したうえで `merge=true` を指定します。自動mergeの判断器をPR headから実行しないことがこのworkflowの信頼境界です。

## 判定とモデル

差分のパスと追加・削除行から、テスト、文書、認証/秘密情報、workflow、runtime、UI、dataなどの語を上限付きで数え、実在する感覚群へ刺激します。既存FlyBrain browser実装の形式と設定に合わせて、leak `0.95`、threshold `1.0`、refractory `3`、最大絶対weightで正規化した基準scale `0.15`、20 tickを使い、下流readout用のdimensionless `synaptic_gain=16` を掛けます。これは固定実dataでedgeあり/なしのreadout差が出る運用校正値で、生物学的な測定値ではありません。

入力binaryはedgeがpre index順で並んでいることを検証してCSR row pointerを構築します。各edgeのpre/post範囲とweightの有限性も全件検証します。判定readoutは直接刺激群を除外し、実edgeを通って到達・発火した下流群だけを使います。欠損する抽象群は `virtual_groups` として明示し、別のニューロンや乱数で水増ししません。

判定は発火したapproach / aversive / motorチャネルの比較だけで、テストの正しさやコードの安全性を証明しません。`reject` でもコードを自動改変せず、`hold` は判断不能です。

## ライセンス

このrepositoryで追加したコードは [MIT License](LICENSE.md) です。connectome dataはMIT Licenseの対象外で、上記のFlyWire CC BY-NC 4.0条件と出典表示が優先します。元のconnectome互換形式と実装背景は [snedea/flybrain](https://github.com/snedea/flybrain/tree/9191824d17871b7851645782d53d23f213ddb938) に帰属します。
