import assert from "node:assert/strict";
import test from "node:test";
import {normalize} from "../web/performance.js";

const step=900;
const samples=[10,30,200].map(base=>[0,1,2,3].map(n=>[n*step,0,0,base,base*(1+n/10),100]));
test("normalized prices use one common open and exclude the unfinished bar",()=>{
  const model=normalize(samples,{step,bars:3},3*step+120);
  assert.deepEqual(model.times,[0,900,1800,2700]);
  for(const line of model.series)assert.ok(Math.abs(line.values.at(-1)-20)<1e-10);
});
test("missing observations break the line; missing common baseline prevents comparison",()=>{
  const sparse=samples.map(book=>book.slice());
  sparse[1].splice(1,1);
  assert.equal(normalize(sparse,{step,bars:3},3*step).series[1].values[2],null);
  sparse[1].shift();
  assert.equal(normalize(sparse,{step,bars:3},3*step),null);
});
