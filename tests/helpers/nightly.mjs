import MODEL from '../../worker/model-v2-data.mjs';
import {modelDigest,TRAINING_BASE,NIGHTLY_SETTINGS_SHA} from '../../worker/nightly-model.mjs';

// Explicit future test data, never a published forecast or training report.
export async function nightlyFixture({tail='aaaaaaaaaaaa',movement=0}={}){
  const model=structuredClone(MODEL);
  Object.assign(model,{status:'candidate',protocol:'nightly-fixed-settings-1',as_of:'2030-01-01',training_cutoff_exclusive:'2029-12-31',
    model_version:'ensemble-candidate-2030-01-01-'+tail});
  delete model.benchmark;
  model.glm.as_of=model.elo.as_of=model.training_cutoff_exclusive;
  model.glm.home_advantage+=movement;
  Object.assign(model.glm.fit,{training_games:1100,latest_training_date:'2029-12-29'});
  Object.assign(model.elo,{training_games:4270,latest_training_date:'2029-12-29'});
  const modelJson=JSON.stringify(model),checksum=await modelDigest(modelJson);
  const report={schemaVersion:1,protocol:'nightly-fixed-settings-1',status:'success',asOf:'2030-01-01',generatedAt:'2030-01-01T00:23:00Z',
    trainingCutoffExclusive:'2029-12-31',latestTrainingMatch:'2029-12-29',trainingMatches:4270,poissonTrainingMatches:1100,
    modelVersion:model.model_version,modelSha256:checksum,settingsSha256:NIGHTLY_SETTINGS_SHA,dataSha256:'c'.repeat(64),codeSha256:'d'.repeat(64),
    evaluation:{commonMatches:110},promotedToDefault:false,prospectiveValidationRequired:true,repository:'FoXsYsOfficial1909/Footpredict',
    runId:'123',runAttempt:'1',sourceCommit:'e'.repeat(40)};
  return {model,modelJson,report,reportUrl:TRAINING_BASE+'latest.json',reportFetchedAt:'2030-01-01T11:59:59Z',
    modelUrl:TRAINING_BASE+'models/'+checksum+'.json',modelFetchedAt:'2030-01-01T12:00:00Z'};
}

export async function readLimited(body,limit){
  const text=await new Response(body).text();
  if(new TextEncoder().encode(text).length>limit)throw new Error('Body too large');
  return text;
}
