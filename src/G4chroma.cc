#include "G4chroma.hh"
#include "GLG4Scint.hh"
#include <G4SteppingManager.hh>
#include <G4OpticalPhysics.hh>
#include <G4EmPenelopePhysics.hh>
#include <G4TrackingManager.hh>
#include <G4TrajectoryContainer.hh>
#include "G4SystemOfUnits.hh"
#include "G4PhysicalConstants.hh"

#include <iostream>

using namespace std;

ChromaPhysicsList::ChromaPhysicsList():  G4VModularPhysicsList()
{
  // default cut value  (1.0mm) 
  defaultCutValue = 1.0*mm;

  // General Physics
  RegisterPhysics( new G4EmPenelopePhysics(0) );
  // Optical Physics w/o Scintillation
  G4OpticalPhysics* opticalPhysics = new G4OpticalPhysics();
  opticalPhysics->Configure(kScintillation,false);
  RegisterPhysics( opticalPhysics );
  // Scintillation (handled by stepping!)
  new GLG4Scint();
}

ChromaPhysicsList::~ChromaPhysicsList()
{
}

void ChromaPhysicsList::SetCuts(){
  //  " G4VUserPhysicsList::SetCutsWithDefault" method sets 
  //   the default cut value for all particle types 
  SetCutsWithDefault();   
}

SteppingAction::SteppingAction()
{
    scint = true;
}

SteppingAction::~SteppingAction()
{
}

void SteppingAction::EnableScint(bool enabled) {
    scint = enabled;
    cout << "Set scintillation to: " << enabled << endl;
}

void SteppingAction::UserSteppingAction(const G4Step *step) {
    
    const G4Track *g4track = step->GetTrack();
    const int trackid = g4track->GetTrackID();
    Track &track = trackmap[trackid];
    if (track.id == -1) {
        track.id = trackid;
        track.parent_id = g4track->GetParentID();
        track.pdg_code = g4track->GetDefinition()->GetPDGEncoding();
        track.weight = g4track->GetWeight();
        track.name = g4track->GetDefinition()->GetParticleName();
        track.appendStepPoint(step->GetPreStepPoint(), step, true);
    }
    track.appendStepPoint(step->GetPostStepPoint(), step);

    if (scint) {
        
        G4VParticleChange * pParticleChange = GLG4Scint::GenericPostPostStepDoIt(step);
        if (!pParticleChange) return;
             
        const size_t nsecondaries = pParticleChange->GetNumberOfSecondaries();
        for (size_t i = 0; i < nsecondaries; i++) { 
            G4Track * tempSecondaryTrack = pParticleChange->GetSecondary(i);
            fpSteppingManager->GetfSecondary()->push_back( tempSecondaryTrack );
        }
        
        pParticleChange->Clear();
        
    }
    
}


void SteppingAction::clearTracking() {
    trackmap.clear();    
}

Track SteppingAction::getTrack(int id) {
    return trackmap[id];
}

int Track::getNumSteps() { 
    return steps.size(); 
}  

void Track::appendStepPoint(const G4StepPoint* point, const G4Step* step, const bool initial) {
    const double len = initial ? 0.0 : step->GetStepLength();
    
    const G4ThreeVector &position = point->GetPosition();
    const double x = position.x();
    const double y = position.y();
    const double z = position.z();
    const double t = point->GetGlobalTime();

    const G4ThreeVector &momentum = point->GetMomentum();
    const double px = momentum.x();
    const double py = momentum.y();
    const double pz = momentum.z();
    const double ke = point->GetKineticEnergy();

    const double edep = step->GetTotalEnergyDeposit();

    const G4VProcess *process = point->GetProcessDefinedStep();
    string procname;
    if (process) {
        procname = process->GetProcessName();
    } else if (step->GetTrack()->GetCreatorProcess()) {
        procname =  step->GetTrack()->GetCreatorProcess()->GetProcessName();
    } else {
        procname = "---";
    }
    
    steps.emplace_back(x,y,z,t,px,py,pz,ke,edep,procname);
}

TrackingAction::TrackingAction() {
}

TrackingAction::~TrackingAction() {
}

int TrackingAction::GetNumPhotons() const {
    return pos.size();
}

void TrackingAction::Clear() {
    pos.clear();
    dir.clear();
    pol.clear();
    wavelength.clear();
    t0.clear();
    parentTrackID.clear();
}

void TrackingAction::PreUserTrackingAction(const G4Track *track) {
    G4ParticleDefinition *particle = track->GetDefinition();
    if (particle->GetParticleName() == "opticalphoton") {
        pos.push_back(track->GetPosition()/mm);
        dir.push_back(track->GetMomentumDirection());
        pol.push_back(track->GetPolarization());
        wavelength.push_back( (h_Planck * c_light / track->GetKineticEnergy()) / nanometer );
        t0.push_back(track->GetGlobalTime() / ns);
        parentTrackID.push_back(track->GetParentID());
        const_cast<G4Track *>(track)->SetTrackStatus(fStopAndKill);
    }
}

#define PhotonCopy(type,name,accessor) \
void TrackingAction::name(type *arr) const { \
    for (unsigned i=0; i < pos.size(); i++) arr[i] = accessor; \
}
    
PhotonCopy(double,GetX,pos[i].x())
PhotonCopy(double,GetY,pos[i].y())
PhotonCopy(double,GetZ,pos[i].z())
PhotonCopy(double,GetDirX,dir[i].x())
PhotonCopy(double,GetDirY,dir[i].y())
PhotonCopy(double,GetDirZ,dir[i].z())
PhotonCopy(double,GetPolX,pol[i].x())
PhotonCopy(double,GetPolY,pol[i].y())
PhotonCopy(double,GetPolZ,pol[i].z())
PhotonCopy(double,GetWavelength,wavelength[i])
PhotonCopy(double,GetT0,t0[i])
PhotonCopy(int,GetParentTrackID,parentTrackID[i])

#include <boost/python.hpp>
#include <pyublas/numpy.hpp>

using namespace boost::python;

#define PhotonAccessor(type,name,accessor) \
pyublas::numpy_vector<type> PTA_##name(const TrackingAction *pta) { \
    pyublas::numpy_vector<type> r(pta->GetNumPhotons()); \
    pta->accessor(&r[0]); \
    return r; \
}

PhotonAccessor(double,GetX,GetX)
PhotonAccessor(double,GetY,GetY)
PhotonAccessor(double,GetZ,GetZ)
PhotonAccessor(double,GetDirX,GetDirX)
PhotonAccessor(double,GetDirY,GetDirY)
PhotonAccessor(double,GetDirZ,GetDirZ)
PhotonAccessor(double,GetPolX,GetPolX)
PhotonAccessor(double,GetPolY,GetPolY)
PhotonAccessor(double,GetPolZ,GetPolZ)
PhotonAccessor(double,GetWave,GetWavelength)
PhotonAccessor(double,GetT0,GetT0)
PhotonAccessor(int,GetParentTrackID,GetParentTrackID)

#define StepAccessor(type,name,stepvar) \
pyublas::numpy_vector<type> PTA_##name(Track *pta) { \
    const vector<Step> &steps = pta->getSteps(); \
    const size_t sz = steps.size(); \
    pyublas::numpy_vector<type> r(sz); \
    for (size_t i = 0; i < sz; i++) r[i] = steps[i].stepvar; \
    return r; \
}
    
StepAccessor(double,getStepX,x)
StepAccessor(double,getStepY,y)
StepAccessor(double,getStepZ,z)
StepAccessor(double,getStepT,t)
StepAccessor(double,getStepPX,px)
StepAccessor(double,getStepPY,py)
StepAccessor(double,getStepPZ,pz)
StepAccessor(double,getStepKE,ke)
StepAccessor(double,getStepEDep,edep)
//StepAccessor(std::string,getStepProcess,procname)

void export_Chroma()
{
  class_<ChromaPhysicsList, ChromaPhysicsList*, bases<G4VModularPhysicsList>, boost::noncopyable > ("ChromaPhysicsList", "EM+Optics physics list")
    .def(init<>())
    ;
    
    
  class_<Track, Track*, boost::noncopyable> ("Track", "Particle track")
    .def(init<>())
    .def_readonly("track_id",&Track::id)
    .def_readonly("parent_track_id",&Track::parent_id)
    .def_readonly("pdg_code",&Track::pdg_code)
    .def_readonly("weight",&Track::weight)
    .def_readonly("name",&Track::name)
    .def("getNumSteps",&Track::getNumSteps)
    .def("getStepX",PTA_getStepX)
    .def("getStepY",PTA_getStepY)
    .def("getStepZ",PTA_getStepZ)
    .def("getStepT",PTA_getStepT)
    .def("getStepPX",PTA_getStepPX)
    .def("getStepPY",PTA_getStepPY)
    .def("getStepPZ",PTA_getStepPZ)
    .def("getStepKE",PTA_getStepKE)
    .def("getStepEDep",PTA_getStepEDep)
    //.def("getStepProcess",PTA_getStepProcess)
    ;  

  class_<SteppingAction, SteppingAction*, bases<G4UserSteppingAction>,
	 boost::noncopyable > ("SteppingAction", "Stepping action for hacking purposes")
    .def(init<>())
    .def("EnableScint",&SteppingAction::EnableScint)
    .def("clearTracking",&SteppingAction::clearTracking)
    .def("getTrack",&SteppingAction::getTrack)
    ;  
  
  class_<TrackingAction, TrackingAction*, bases<G4UserTrackingAction>,
	 boost::noncopyable > ("TrackingAction", "Tracking action that saves photons")
    .def(init<>())
    .def("GetNumPhotons", &TrackingAction::GetNumPhotons)
    .def("Clear", &TrackingAction::Clear)
    .def("GetX", PTA_GetX)
    .def("GetY", PTA_GetY)
    .def("GetZ", PTA_GetZ)
    .def("GetDirX", PTA_GetDirX)
    .def("GetDirY", PTA_GetDirY)
    .def("GetDirZ", PTA_GetDirZ)
    .def("GetPolX", PTA_GetPolX)
    .def("GetPolY", PTA_GetPolY)
    .def("GetPolZ", PTA_GetPolZ)
    .def("GetWavelength", PTA_GetWave)
    .def("GetT0", PTA_GetT0)
    .def("GetParentTrackID", PTA_GetParentTrackID)
    ;
}

BOOST_PYTHON_MODULE(_g4chroma)
{
  export_Chroma();
}
