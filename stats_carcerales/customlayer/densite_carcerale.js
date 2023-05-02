
const LAYER_URL = 'https://gis.jdev.fr/geoserver/prison/ows?service=WFS&version=1.0.0&request=GetFeature&typeName=prison:densite_carcerale_etab&outputFormat=application%2Fjson';
const LAYER_ID = "densite_carcerale";

var myStyle = new ol.style.Style({
    image: new ol.style.Circle({
      radius: 7,
      fill: new ol.style.Fill({color: 'black'}),
      stroke: new ol.style.Stroke({
        color: [0,0,0], width: 2
      })
    })
})

/**
     * Create OpenLayers style
     * @param {Integer} r - Radius 
     * * @param {Integer} size - value to display
     * @param {String} color - as rgb, rgba or simple color name
*/
function layerStyle (r, size, color) {
    var style = new ol.style.Style({
        image: new ol.style.Circle({
            radius: r,
            fill: new ol.style.Fill({
                color: color
            }),
            stroke: new ol.style.Stroke({
                color: 'white',
                width: 2
            })
        })
    });
    return [style];        
}

function getColor(s){
    if(s <= 0.5 && s) {
        color = 'rgba(222, 245, 229, 0.9)';
    } else if(s <= 1) {
        color = 'rgba(75, 194, 173, 0.9)';
    } else if(s <= 1.5) {
        color = 'rgba(53, 123, 163, 0.9)';
    } else if(s <= 2) {
        color = 'rgba(62, 53, 107, 0.9)';
    } else {
        color = 'rgba(11, 4, 5, 0.9)';
    }
    return color;
}

// get proportional style
function getStyle (feature) {
    var size,
    radius;
    var max_radius = 40;
    var max_value = 4000;
    size = feature.getProperties().ecroue_detenu;
    densite = feature.getProperties().densite_car;
    var colors = getColor(densite);
    // radius
    radius = Math.sqrt(size/max_value) * max_radius;
    return layerStyle(radius, size, colors);
};

let layer = new ol.layer.Vector({
    source: new ol.source.Vector({
        url: LAYER_URL,
        format: new ol.format.GeoJSON()
    }),
    style: getStyle
});



var densite_carcerale = new CustomLayer(LAYER_ID, layer, legend);

