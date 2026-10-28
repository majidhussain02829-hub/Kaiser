"""Customer quote view. Jinja autoescaping keeps all saved text inert."""
TEMPLATE = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Quote — {{ business }}</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f3f6f3;color:#18342d;font:16px/1.6 Arial,sans-serif}.tools{max-width:850px;margin:24px auto;display:flex;gap:18px;align-items:center;padding:0 24px}.tools a{color:#225b45}button{background:#225b45;color:white;border:0;border-radius:8px;padding:12px 18px;font:inherit;cursor:pointer}article{max-width:850px;margin:24px auto;padding:48px;background:white;border:1px solid #dce5dc;border-radius:12px}header{border-bottom:3px solid #225b45;padding-bottom:24px}h1{font-size:36px;margin:0}h2{font-size:18px;margin:28px 0 8px}.muted{color:#617265}.details{display:grid;grid-template-columns:1fr 1fr;gap:20px}.copy{white-space:pre-wrap;overflow-wrap:anywhere}.total{margin-top:32px;padding:24px;background:#e9f0e4;display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap}.total strong{font-size:28px}footer{border-top:1px solid #dce5dc;margin-top:32px;padding-top:20px;font-size:13px}p{margin:6px 0}small{overflow-wrap:anywhere}@media(max-width:600px){article{padding:24px;margin:12px}.details{grid-template-columns:1fr}.tools{flex-wrap:wrap}}@page{size:A4;margin:18mm}@media print{body{background:white}.tools{display:none}article{margin:0;padding:0;border:0;max-width:none}.total{break-inside:avoid}h2{break-after:avoid}a{color:inherit;text-decoration:none}}
</style></head><body>
<div class="tools"><a href="/">← Back to Quote Desk</a><button onclick="window.print()">Print / save PDF</button><span class="muted">Review before sharing.</span></div>
<article><header><p class="muted">{{ business }}</p><h1>Quote</h1><small>Reference {{ entry.id }} · Revision {{ entry.version }}</small></header>
<div class="details"><section><h2>Prepared for</h2><p>{{ entry.name }}</p><p>{{ entry.suburb }}</p>{% if entry.email %}<p class="copy">{{ entry.email }}</p>{% endif %}</section><section><h2>Prepared by</h2><p>{{ business }}</p><p class="copy">{{ contact }}</p><p>{{ entry.service }}</p></section></div>
<h2>Scope of work</h2><div class="copy">{{ entry.quote_scope }}</div>
{% if entry.quote_terms %}<h2>Terms and inclusions</h2><div class="copy">{{ entry.quote_terms }}</div>{% endif %}
<div class="total"><span>Total quoted · AUD</span><strong>A${{ total }}</strong></div>
<footer><p>Please contact {{ business }} at {{ contact }} to discuss or accept this quote.</p><p>This is a quote, not a tax invoice or payment receipt.</p></footer>
</article></body></html>'''
